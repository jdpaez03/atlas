"""Live missions: ATLAS plans, delegates, supervises, reviews and consolidates on the Claude API.

    OBJECTIVE → DECOMPOSITION (create_plan, one validation retry) → DELEGATION (tasks in the store)
    → EXECUTION (DAG scheduler; COLLABORATION once agents consult each other) → VALIDATION (review,
    at most one round of ≤3 follow-ups) → CONSOLIDATION (submit_mission_report) → REPORTING
    → FOLLOW_UP → CLOSED, then every touched agent goes back to its default status.

A failed task (LLM/API error after the retry) goes FAILED, its agent ERROR, and the mission goes on;
the mission report lists the failures. `LiveEngine` owns the running missions and can cancel them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.models import (
    AgentStatus,
    ApprovalReason,
    Claim,
    ClaimKind,
    Mission,
    MissionPhase,
    MissionReport,
    Priority,
    TaskStatus,
)
from ..core.store import WorldStore
from .agent_loader import AgentLoader, ResolvedAgent
from .context import NodeContext
from .llm import LLMClient, LLMError, Meter, block_to_param, tool_uses
from .pricing import PriceTable
from .prompts import (
    ORCHESTRATOR,
    SUBMIT_MISSION_REPORT_TOOL,
    consolidation_message,
    context_block,
    create_plan_tool,
    followups_tool,
    plan_errors_message,
    planning_message,
    render_report,
    review_message,
    system_blocks,
)
from .runtime import (
    AgentRun,
    LiveConfig,
    MissionScope,
    _enum,
    _short,
    _str_list,
    dependency_inputs,
    to_claims,
)

log = logging.getLogger("atlas.live")

_TERMINAL = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
_DEAD = {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
_PRIO_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MAX_FOLLOWUPS = 3


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


def validate_plan(
    data: Any,
    allowed: set[str],
    *,
    existing_refs: set[str] | frozenset[str] = frozenset(),
    min_tasks: int = 2,
    max_tasks: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a create_plan / request_followups input. Returns (tasks in topological order, errors)."""
    raw = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], ["'tasks' must be a list"]
    errors: list[str] = []
    if not min_tasks <= len(raw) <= max_tasks:
        errors.append(f"the plan must have between {min_tasks} and {max_tasks} tasks (got {len(raw)})")
    tasks: list[dict[str, Any]] = []
    refs: set[str] = set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            errors.append(f"task #{i + 1} is not an object")
            continue
        ref = str(t.get("ref") or "").strip()
        label = f"task '{ref}'" if ref else f"task #{i + 1}"
        if not ref:
            errors.append(f"{label}: missing ref")
        elif ref in refs or ref in existing_refs:
            errors.append(f"{label}: duplicate ref")
        refs.add(ref)
        title = str(t.get("title") or "").strip()
        desc = str(t.get("description") or "").strip()
        if not title:
            errors.append(f"{label}: missing title")
        agent = str(t.get("assigned_to") or "").strip()
        if agent not in allowed:
            errors.append(
                f"{label}: agent '{agent}' is not in the roster (allowed: {', '.join(sorted(allowed))})"
            )
        deps = _str_list(t.get("depends_on"))
        needs_approval = bool(t.get("requires_approval", False))
        reason = t.get("approval_reason")
        tasks.append({
            "ref": ref,
            "title": title,
            "description": desc or title,
            "assigned_to": agent,
            "priority": _enum(t.get("priority"), Priority, Priority.MEDIUM).value,
            "depends_on": deps,
            "requires_approval": needs_approval,
            "approval_reason": (
                _enum(reason, ApprovalReason, ApprovalReason.CONSEQUENTIAL_DECISION).value
                if needs_approval else None
            ),
        })
    for t in tasks:
        for d in t["depends_on"]:
            if d == t["ref"]:
                errors.append(f"task '{t['ref']}' depends on itself")
            elif d not in refs and d not in existing_refs:
                errors.append(f"task '{t['ref']}' depends on unknown ref '{d}'")
    if errors:
        return [], errors
    # topological order (Kahn); leftovers are a cycle
    ordered: list[dict[str, Any]] = []
    placed = set(existing_refs)
    todo = list(tasks)
    while todo:
        ready = [t for t in todo if all(d in placed for d in t["depends_on"])]
        if not ready:
            return [], ["dependency cycle between: " + ", ".join(t["ref"] for t in todo)]
        for t in ready:
            ordered.append(t)
            placed.add(t["ref"])
            todo.remove(t)
    return ordered, []


def _roster_entry(r: ResolvedAgent) -> dict[str, Any]:
    a = r.agent
    entry: dict[str, Any] = {"id": a.id, "name": a.name, "title": a.title, "description": a.description,
                             "capabilities": a.capabilities}
    if a.division:
        entry["division"] = a.division
    return entry


# ---------------------------------------------------------------------------
# One mission
# ---------------------------------------------------------------------------


class LiveMission:
    def __init__(self, engine: LiveEngine, mission: Mission):
        self.engine = engine
        self.store: WorldStore = engine.store
        self.mission_id = mission.id
        self.config = engine.config
        orchestrator = engine.loader.resolve(self.store.registry.orchestrator.id)
        roster = engine.loader.roster(mission.node)
        self.scope = MissionScope(
            store=self.store,
            meter=Meter(engine.llm, self.store, mission.id, engine.prices),
            config=self.config,
            context=engine.context,
            mission_id=mission.id,
            objective=mission.objective,
            node=mission.node,
            agents={r.id: r for r in roster},
            orchestrator=orchestrator,
        )
        self.refs: dict[str, str] = {}  # plan ref -> task id
        self.failures: list[str] = []
        self._active: dict[str, list[str]] = {}  # agent id -> running task ids
        self._children: set[asyncio.Task[None]] = set()

    @property
    def atlas(self) -> str:
        return self.scope.orchestrator.id

    @property
    def roster(self) -> list[ResolvedAgent]:
        return list(self.scope.agents.values())

    def _system(self) -> list[dict[str, Any]]:
        o = self.scope.orchestrator
        return system_blocks(o.role_prompt, ORCHESTRATOR, context_block(self.scope.context_for(o.agent)))

    async def _atlas(self, status: AgentStatus, activity: str) -> None:
        await self.scope.set_agent(self.atlas, status, activity=activity)

    async def _phase(self, phase: MissionPhase) -> None:
        await self.store.set_phase(self.mission_id, phase)

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._flow()
        except asyncio.CancelledError:
            await self._cancel_children()
            raise
        except Exception as exc:
            log.exception("live mission %s failed", self.mission_id)
            await self._cancel_children()
            try:
                await self._abort(f"Unexpected error: {exc}")
            except Exception:  # pragma: no cover - never let cleanup mask the original error
                log.exception("could not close mission %s", self.mission_id)
        await self.store.release_agents(self.mission_id, self.scope.touched)

    async def _cancel_children(self) -> None:
        for t in list(self._children):
            t.cancel()
        for t in list(self._children):
            try:
                await t
            except asyncio.CancelledError:
                continue
            except Exception:  # already reported by _run_task
                log.debug("child task ended with an error", exc_info=True)
        self._children.clear()

    async def _flow(self) -> None:
        s = self.store
        await self._atlas(AgentStatus.WORKING, "Analyzing the objective")
        await self._phase(MissionPhase.DECOMPOSITION)
        if not self.roster:
            await self._abort(f"No agents are available for node '{self.scope.node}'.")
            return
        await self._atlas(AgentStatus.WORKING, "Decomposing the objective into tasks")
        plan = await self._plan()
        if plan is None:
            return

        await self._phase(MissionPhase.DELEGATION)
        await self._atlas(AgentStatus.WORKING, f"Delegating {len(plan)} tasks")
        ids = await self._create_tasks(plan)
        await self._mark_waiting(ids)

        await self._phase(MissionPhase.EXECUTION)
        await self._atlas(AgentStatus.REVIEWING, f"Supervising {len(ids)} tasks")
        await self._execute(ids)

        await self._phase(MissionPhase.VALIDATION)
        n = len(s.reports_for(self.mission_id))
        await self._atlas(AgentStatus.REVIEWING, f"Reviewing {n} agent reports")
        followups = await self._review()
        if followups:
            await s.log(
                f"ATLAS opened {len(followups)} follow-up task(s)", mission_id=self.mission_id, agent_id=self.atlas
            )
            fids = await self._create_tasks(followups)
            await self._mark_waiting(fids)
            await self._atlas(AgentStatus.REVIEWING, f"Supervising {len(fids)} follow-up tasks")
            await self._execute(fids)

        await self._phase(MissionPhase.CONSOLIDATION)
        n = len(s.reports_for(self.mission_id))
        await self._atlas(AgentStatus.WORKING, f"Consolidating {n} agent reports")
        report = await self._consolidate()

        await self._phase(MissionPhase.REPORTING)
        await self._atlas(AgentStatus.WORKING, "Writing executive report")
        await s.submit_mission_report(report)
        await self._phase(MissionPhase.FOLLOW_UP)
        await self._atlas(AgentStatus.COMPLETED, "Mission report delivered")
        await self._phase(MissionPhase.CLOSED)

    async def _abort(self, reason: str) -> None:
        """Close the mission with a NOT_ACHIEVED report (planning impossible, unexpected error)."""
        s = self.store
        if s.mission(self.mission_id).phase == MissionPhase.CLOSED.value:
            return
        for t in s.tasks_for(self.mission_id):
            if t.status not in _TERMINAL:
                await s.update_task(t.id, status=TaskStatus.CANCELLED)
        await s.log(f"ATLAS could not complete the mission · {reason}", mission_id=self.mission_id,
                    agent_id=self.atlas)
        await self._atlas(AgentStatus.ERROR, _short(reason, 120))
        report = self._base_report(
            executive_summary=reason,
            objective_status="NOT_ACHIEVED",
            needs_human_attention=[reason],
        )
        await self._phase(MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        await self._phase(MissionPhase.CLOSED)

    # -- planning ------------------------------------------------------------

    async def _plan(self) -> list[dict[str, Any]] | None:
        allowed = set(self.scope.agents)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": planning_message(self.scope.objective, self.scope.node,
                                        [_roster_entry(r) for r in self.roster]),
        }]
        errors: list[str] = []
        for attempt in range(2):
            try:
                resp = await self.scope.meter.create(
                    model=self.scope.orchestrator.model,
                    max_tokens=self.config.orchestrator_max_tokens,
                    system=self._system(),
                    tools=[create_plan_tool(sorted(allowed))],
                    tool_choice={"type": "tool", "name": "create_plan"},
                    messages=messages,
                )
            except LLMError as exc:
                await self._abort(f"Planning failed: {exc}")
                return None
            uses = tool_uses(resp)
            plan_use = next((u for u in uses if u.name == "create_plan"), None)
            if plan_use is None:
                tasks, errors = [], ["you must call create_plan"]
            else:
                tasks, errors = validate_plan(plan_use.input, allowed)
            if not errors:
                rationale = str((plan_use.input or {}).get("rationale") or "").strip()
                await self.store.log(
                    f"ATLAS planned {len(tasks)} tasks" + (f" · {_short(rationale, 140)}" if rationale else ""),
                    mission_id=self.mission_id, agent_id=self.atlas,
                )
                return tasks
            await self.store.log(
                f"ATLAS plan rejected ({'retrying' if attempt == 0 else 'giving up'}) · {'; '.join(errors)}",
                mission_id=self.mission_id, agent_id=self.atlas,
            )
            if attempt == 0:
                content = [block_to_param(b) for b in resp.content]
                if content:
                    messages.append({"role": "assistant", "content": content})
                results = [
                    {"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                     "content": plan_errors_message(errors) if u is plan_use else "Ignored."}
                    for u in uses
                ]
                if not results:
                    results = [{"type": "text", "text": plan_errors_message(errors)}]
                messages.append({"role": "user", "content": results})
        await self._abort("ATLAS could not produce a valid plan: " + "; ".join(errors))
        return None

    async def _create_tasks(self, plan: list[dict[str, Any]]) -> list[str]:
        ids = []
        for t in plan:
            deps = [self.refs[d] for d in t["depends_on"]]
            task = await self.store.create_task(
                self.mission_id, t["title"], t["description"], t["assigned_to"],
                created_by=self.atlas, priority=t["priority"], depends_on=deps,
                requires_approval=t["requires_approval"], approval_reason=t["approval_reason"],
            )
            self.refs[t["ref"]] = task.id
            ids.append(task.id)
        return ids

    async def _mark_waiting(self, ids: list[str]) -> None:
        by_agent: dict[str, list[str]] = {}
        for tid in ids:
            t = self.store.task(tid)
            if t.assigned_to:
                by_agent.setdefault(t.assigned_to, []).append(tid)
        for agent_id, tids in by_agent.items():
            tasks = [self.store.task(t) for t in tids]
            if self._active.get(agent_id) or any(t.status == TaskStatus.READY.value for t in tasks):
                continue
            first = tasks[0]
            deps = ", ".join(f"'{self.store.task(d).title}'" for d in first.depends_on)
            await self.scope.set_agent(agent_id, AgentStatus.WAITING, activity=f"Waiting for {deps}",
                                       task_id=first.id)

    # -- execution -----------------------------------------------------------

    async def _execute(self, ids: list[str]) -> None:
        pending = set(ids)
        running: dict[asyncio.Task[None], str] = {}
        while True:
            changed = True
            while changed:
                changed = False
                order = sorted(pending, key=lambda i: _PRIO_ORDER.get(self.store.task(i).priority, 2))
                for tid in order:
                    t = self.store.task(tid)
                    if t.status == TaskStatus.READY.value:
                        child = asyncio.create_task(self._run_task(tid), name=f"live-task:{tid}")
                        self._children.add(child)
                        child.add_done_callback(self._children.discard)
                        running[child] = tid
                        pending.discard(tid)
                    elif t.status in _TERMINAL:
                        pending.discard(tid)
                    elif any(self.store.task(d).status in _DEAD for d in t.depends_on):
                        dead = next(self.store.task(d) for d in t.depends_on
                                    if self.store.task(d).status in _DEAD)
                        await self.store.update_task(tid, status=TaskStatus.CANCELLED)
                        await self.store.log(
                            f"'{t.title}' cancelled: its dependency '{dead.title}' {dead.status.lower()}",
                            mission_id=self.mission_id, agent_id=self.atlas,
                        )
                        pending.discard(tid)
                        changed = True
            if not running:
                for tid in pending:  # unreachable in a valid DAG; never hang
                    await self.store.update_task(tid, status=TaskStatus.CANCELLED)
                return
            done, _ = await asyncio.wait(list(running), return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                running.pop(d, None)

    async def _run_task(self, task_id: str) -> None:
        s = self.store
        task = s.task(task_id)
        agent_id = task.assigned_to or ""
        async with self.engine.agent_slot(agent_id), self.engine.global_slot:
            task = s.task(task_id)
            if task.status != TaskStatus.READY.value:
                return
            self._active.setdefault(agent_id, []).append(task_id)
            try:
                run = AgentRun(self.scope, task, dependency_inputs(self.scope, task, self.refs))
                await run.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("task %s failed: %s", task_id, exc, exc_info=True)
                self.failures.append(f"Task '{task.title}' ({self.scope.name(agent_id)}) failed: {exc}")
                if s.task(task_id).status not in _TERMINAL:
                    await s.update_task(task_id, status=TaskStatus.FAILED)
                await self.scope.set_agent(agent_id, AgentStatus.ERROR,
                                           activity=_short(f"Failed '{task.title}': {exc}", 140), task_id=task_id)
                await s.log(f"{self.scope.name(agent_id)} failed '{task.title}' · {_short(str(exc), 160)}",
                            mission_id=self.mission_id, agent_id=agent_id)
                return
            finally:
                self._active[agent_id].remove(task_id)
            others = self._active.get(agent_id) or []
            if others:
                other = s.task(others[0])
                await self.scope.set_agent(agent_id, AgentStatus.WORKING, activity=f"Working on '{other.title}'",
                                           task_id=other.id)
            else:
                await self.scope.set_agent(agent_id, AgentStatus.COMPLETED,
                                           activity=f"Delivered '{task.title}'", task_id=task_id)

    # -- review + consolidation ---------------------------------------------

    def _tasks_text(self) -> str:
        ref_of = {v: k for k, v in self.refs.items()}
        return "\n".join(
            f"- {ref_of.get(t.id, t.id)} · {t.title} · {self.scope.name(t.assigned_to or '')} · {t.status}"
            for t in self.store.tasks_for(self.mission_id)
        )

    def _reports_text(self) -> list[str]:
        ref_of = {v: k for k, v in self.refs.items()}
        out = []
        for r in self.store.reports_for(self.mission_id):
            t = self.store.task(r.task_id)
            out.append(render_report(r, title=t.title, agent_name=self.scope.name(r.agent_id),
                                     ref=ref_of.get(t.id)))
        return out

    async def _review(self) -> list[dict[str, Any]]:
        allowed = set(self.scope.agents)
        try:
            resp = await self.scope.meter.create(
                model=self.scope.orchestrator.model,
                max_tokens=self.config.orchestrator_max_tokens,
                system=self._system(),
                tools=[followups_tool(sorted(allowed))],
                tool_choice={"type": "tool", "name": "request_followups"},
                messages=[{"role": "user", "content": review_message(
                    self.scope.objective, self._tasks_text(), self._reports_text(), self.failures,
                    [_roster_entry(r) for r in self.roster])}],
            )
        except LLMError as exc:
            await self.store.log(f"Review skipped · {exc}", mission_id=self.mission_id, agent_id=self.atlas)
            return []
        use = next((u for u in tool_uses(resp) if u.name == "request_followups"), None)
        if use is None:
            return []
        assessment = str((use.input or {}).get("assessment") or "").strip()
        if assessment:
            await self.store.log(f"ATLAS review · {_short(assessment, 200)}", mission_id=self.mission_id,
                                 agent_id=self.atlas)
        tasks, errors = validate_plan(use.input, allowed, existing_refs=set(self.refs), min_tasks=0,
                                      max_tasks=MAX_FOLLOWUPS)
        if errors:
            await self.store.log(f"Follow-ups skipped (invalid) · {'; '.join(errors)}",
                                 mission_id=self.mission_id, agent_id=self.atlas)
            return []
        return tasks

    def _base_report(self, **fields: Any) -> MissionReport:
        s = self.store
        tasks = s.tasks_for(self.mission_id)
        done = TaskStatus.COMPLETED.value
        attention = list(fields.pop("needs_human_attention", []))
        for f in self.failures:
            if f not in attention:
                attention.append(f)
        return MissionReport(
            mission_id=self.mission_id,
            tasks_completed=[t.title for t in tasks if t.status == done],
            tasks_pending=[t.title for t in tasks if t.status != done],
            agent_report_ids=[r.id for r in s.reports_for(self.mission_id)],
            needs_human_attention=attention,
            **fields,
        )

    async def _consolidate(self) -> MissionReport:
        try:
            resp = await self.scope.meter.create(
                model=self.scope.orchestrator.model,
                max_tokens=self.config.orchestrator_max_tokens,
                system=self._system(),
                tools=[SUBMIT_MISSION_REPORT_TOOL],
                tool_choice={"type": "tool", "name": "submit_mission_report"},
                messages=[{"role": "user", "content": consolidation_message(
                    self.scope.objective, self._tasks_text(), self._reports_text(), self.failures)}],
            )
            use = next((u for u in tool_uses(resp) if u.name == "submit_mission_report"), None)
            if use is None:
                raise LLMError("ATLAS did not call submit_mission_report")
        except LLMError as exc:
            await self.store.log(f"Consolidation fell back to an automatic summary · {exc}",
                                 mission_id=self.mission_id, agent_id=self.atlas)
            return self._fallback_report(str(exc))
        data = use.input or {}
        status = str(data.get("objective_status") or "").upper()
        if status not in ("ACHIEVED", "PARTIAL", "NOT_ACHIEVED"):
            status = "PARTIAL"
        return self._base_report(
            executive_summary=str(data.get("executive_summary") or "").strip() or "(no summary)",
            objective_status=status,
            key_findings=to_claims(data.get("key_findings")),
            conflicts=_str_list(data.get("conflicts")),
            assumptions=_str_list(data.get("assumptions")),
            needs_human_attention=_str_list(data.get("needs_human_attention")),
            next_actions=_str_list(data.get("next_actions")),
            references=_str_list(data.get("references")),
        )

    def _fallback_report(self, why: str) -> MissionReport:
        reports = self.store.reports_for(self.mission_id)
        findings: list[Claim] = []
        for r in reports:
            findings += [c for c in r.findings if c.kind == ClaimKind.RECOMMENDATION.value][:2]
        done = [t for t in self.store.tasks_for(self.mission_id) if t.status == TaskStatus.COMPLETED.value]
        return self._base_report(
            executive_summary=(
                f"Automatic consolidation ({len(reports)} agent reports): ATLAS could not write the executive "
                "summary. Review the agent reports directly."
            ),
            objective_status="PARTIAL" if done else "NOT_ACHIEVED",
            key_findings=findings[:10],
            needs_human_attention=[f"Consolidation failed: {why}"],
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LiveEngine:
    """Starts, tracks and cancels live missions. Concurrency caps are shared by all missions."""

    def __init__(
        self,
        store: WorldStore,
        *,
        loader: AgentLoader | None = None,
        context: NodeContext | None = None,
        config: LiveConfig | None = None,
        llm: LLMClient | None = None,
        prices: PriceTable | None = None,
    ):
        self.store = store
        self.config = config or LiveConfig.from_env()
        self.loader = loader or AgentLoader(store.registry, self.config.models)
        self.context = context or NodeContext(store.registry)
        self.prices = prices or PriceTable.from_env()
        self._llm = llm
        self.global_slot = asyncio.Semaphore(self.config.max_concurrency)
        self._agent_slots: dict[str, asyncio.Semaphore] = {}
        self._running: dict[str, tuple[LiveMission, asyncio.Task[None]]] = {}

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            from .llm import AnthropicLLM

            self._llm = AnthropicLLM()
        return self._llm

    @llm.setter
    def llm(self, value: LLMClient) -> None:
        self._llm = value

    def agent_slot(self, agent_id: str) -> asyncio.Semaphore:
        if agent_id not in self._agent_slots:
            limit = max(1, self.store.registry.get(agent_id).permissions.max_parallel_tasks)
            self._agent_slots[agent_id] = asyncio.Semaphore(limit)
        return self._agent_slots[agent_id]

    async def start(self, objective: str, node: str) -> Mission:
        mission = await self.store.create_mission(objective, node, mode="live")
        live = LiveMission(self, mission)
        task = asyncio.create_task(live.run(), name=f"live-mission:{mission.id}")
        self._running[mission.id] = (live, task)
        task.add_done_callback(lambda _t, mid=mission.id: self._running.pop(mid, None))
        return mission

    def mission(self, mission_id: str) -> LiveMission | None:
        entry = self._running.get(mission_id)
        return entry[0] if entry else None

    def running(self) -> list[str]:
        return list(self._running)

    async def wait(self, mission_id: str) -> None:
        entry = self._running.get(mission_id)
        if entry:
            await asyncio.shield(entry[1])

    async def cancel(self, mission_id: str, reason: str = "cancelled by the user") -> bool:
        entry = self._running.get(mission_id)
        if entry is None:
            return False
        live, task = entry
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover
            log.exception("live mission %s raised while cancelling", mission_id)
        if self.store.mission(mission_id).phase != MissionPhase.CLOSED.value:
            await self.store.cancel_mission(mission_id, reason)
        await self.store.release_agents(mission_id, live.scope.touched)
        return True

    async def cancel_all(self) -> None:
        for mid in list(self._running):
            try:
                await self.cancel(mid, "ATLAS is shutting down")
            except Exception:  # pragma: no cover - best effort at shutdown/reset
                log.exception("could not cancel live mission %s", mid)
        self._running.clear()
