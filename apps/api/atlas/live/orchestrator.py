"""Live missions: ATLAS plans, delegates, supervises, reviews and consolidates on the Claude API.

    OBJECTIVE → DECOMPOSITION (create_plan, one validation retry) → DELEGATION (tasks in the store)
    → EXECUTION (DAG scheduler; COLLABORATION once agents consult each other) → VALIDATION (review,
    at most one round of ≤3 follow-ups) → CONSOLIDATION (submit_mission_report) → REPORTING
    → FOLLOW_UP → CLOSED, then every touched agent goes back to its default status.

A failed task (LLM/API error after the retry) goes FAILED, its agent ERROR, and the mission goes on;
the mission report lists the failures. `LiveEngine` owns the running missions and can cancel them.

Mission thread (docs/PHASE3.md B): `LiveEngine.post_message` records the human's note. While the mission
runs, the note becomes "human guidance" for every task / review / consolidation that starts afterwards and
ATLAS acknowledges it (fast model). On a closed (or interrupted) mission it opens a follow-up: ATLAS calls
`respond_to_followup {answer?, tasks?}`; tasks start round N (DELEGATION → … → CLOSED, Task.round = N) and
end with MissionReport version N (earlier versions stay).

Phase 5 (docs/AUDITOR.md): after execution, AUDITOR checks every report of the round against the evidence ATLAS
recorded; a FAIL goes back to its agent for one revision (Task.revision_of), audited again as final. The mission
report carries `audit_summary` and `untraced` (figures that appear in no agent report). A task that hits a
transient error is re-run (`ATLAS_TASK_RETRIES`, backoff `ATLAS_TASK_RETRY_DELAY`); a closed mission with failed
or cancelled tasks can be resumed (`LiveEngine.resume`): they run again as a new round with a new report version.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.models import (
    HUMAN,
    AgentMessage,
    AgentReport,
    AgentStatus,
    ApprovalReason,
    Attachment,
    Audit,
    AuditVerdict,
    Claim,
    ClaimKind,
    MessageType,
    Mission,
    MissionPhase,
    MissionReport,
    Priority,
    Task,
    TaskStatus,
)
from ..core.registry import RegistryError
from ..core.store import WorldStore
from . import auditor as audit_mod
from .agent_loader import AgentLoader, ResolvedAgent
from .backend import BackendInfo, detect_backend
from .context import NodeContext
from .executor import ApiExecutor, Executor
from .llm import LLMClient, LLMError, Meter, UsageLimitError, current_agent
from .pricing import PriceTable
from .prompts import (
    ACKNOWLEDGE_TOOL,
    ORCHESTRATOR,
    SUBMIT_MISSION_REPORT_TOOL,
    acknowledge_message,
    consolidation_message,
    context_block,
    create_plan_tool,
    followup_consolidation_note,
    followup_message,
    followups_tool,
    planning_message,
    render_mission_report,
    render_report,
    respond_to_followup_tool,
    review_message,
    with_mission_notes,
)
from .runtime import (
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
EXTERNAL_ADAPTERS = {"http", "cli"}  # docs/AGENTS.md "External agents"; mcp stays unavailable
_PRIO_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MAX_FOLLOWUPS = 3
MAX_ROUND_TASKS = 6  # tasks ATLAS may open for one follow-up round from the mission thread
ACK_FALLBACK = "Noted. I'll apply this to the work that starts from now on."
AUDIT_CONSOLIDATION_NOTE = (
    "AUDITOR checked the reports (see each AUDIT line). Build on what held up. Where a report was sent back and "
    "revised, use the revision. Never present a finding AUDITOR marked unsupported or mislabeled as a FACT: "
    "drop it, relabel it, or list it under needs_human_attention. Every figure you write must come from a report."
)


def _retryable(exc: BaseException) -> bool:
    """Transient failures worth one more try: API/LLM errors (not the plan's usage limit), network, timeouts,
    and external agents that failed to answer."""
    if isinstance(exc, UsageLimitError):
        return False
    if isinstance(exc, (LLMError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    if type(exc).__name__ in ("ExternalAgentError", "ReadTimeout", "ConnectTimeout", "ConnectError",
                              "RemoteProtocolError"):
        return True
    return isinstance(exc, OSError)


def _resolve_auditor(loader: AgentLoader, config: LiveConfig) -> ResolvedAgent | None:
    if not config.audit:
        return None
    try:
        agent = loader.resolve("auditor")
    except RegistryError:  # not registered (custom agents dir): no audit, missions still run
        return None
    return agent if agent.available else None


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
    def __init__(self, engine: LiveEngine, mission: Mission, backend: str = "api"):
        self.engine = engine
        self.store: WorldStore = engine.store
        self.mission_id = mission.id
        self.backend = backend
        self.config = engine.config_for(backend)
        self.executor: Executor = engine.executor(backend)
        loader = engine.loader_for(backend)
        orchestrator = loader.resolve(self.store.registry.orchestrator.id)
        roster = loader.roster(mission.node)
        auditor = _resolve_auditor(loader, self.config)
        self.scope = MissionScope(
            store=self.store,
            meter=Meter(engine.llm if backend == "api" else None, self.store, mission.id, engine.prices,
                        default_agent=orchestrator.id),
            config=self.config,
            context=engine.context,
            mission_id=mission.id,
            objective=mission.objective,
            node=mission.node,
            agents={r.id: r for r in roster},
            orchestrator=orchestrator,
            auditor=auditor,
        )
        self.refs: dict[str, str] = {}  # plan ref -> task id
        for i, tid in enumerate(mission.task_ids):  # earlier rounds (follow-ups): T1, T2...
            self.refs[f"T{i + 1}"] = tid
        self.round = mission.round
        self.followup_request: str | None = None  # the human's message that opened this round
        self.failures: list[str] = []
        self.limited: str | None = None  # plan usage limit hit: no more LLM calls in this mission
        self._active: dict[str, list[str]] = {}  # agent id -> running task ids
        self._children: set[asyncio.Task[None]] = set()
        self.audit_skipped: str | None = None  # why the audit of this round didn't run, if it didn't

    @property
    def atlas(self) -> str:
        return self.scope.orchestrator.id

    @property
    def roster(self) -> list[ResolvedAgent]:
        return list(self.scope.agents.values())

    def _system(self) -> list[str]:
        o = self.scope.orchestrator
        return [o.role_prompt, ORCHESTRATOR, context_block(self.scope.context_for(o.agent))]

    async def _atlas_step(self, tool: dict[str, Any], prompt: str, **kw: Any) -> dict[str, Any] | None:
        token = current_agent.set(self.atlas)
        try:
            return await self.executor.structured(
                self.scope, model=self.scope.orchestrator.model, system=self._system(), prompt=prompt, tool=tool,
                max_tokens=self.config.orchestrator_max_tokens, **kw,
            )
        finally:
            current_agent.reset(token)

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

        await self._audit_round()

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
        prompt = with_mission_notes(
            planning_message(self.scope.objective, self.scope.node, [_roster_entry(r) for r in self.roster]),
            self._notes(), self._attachment_names(),
        )
        errors: list[str] = []
        tasks: list[dict[str, Any]] = []
        attempts = 0

        async def validate(data: dict[str, Any] | None) -> list[str]:
            nonlocal errors, tasks, attempts
            attempts += 1
            if data is None:
                tasks, errors = [], ["you must call create_plan"]
            else:
                tasks, errors = validate_plan(data, allowed)
            if errors:
                await self.store.log(
                    f"ATLAS plan rejected ({'retrying' if attempts == 1 else 'giving up'}) · {'; '.join(errors)}",
                    mission_id=self.mission_id, agent_id=self.atlas,
                )
            return errors

        try:
            data = await self._atlas_step(create_plan_tool(sorted(allowed)), prompt, validate=validate, attempts=2)
        except LLMError as exc:
            await self._abort(f"Planning failed: {exc}")
            return None
        if data is None or errors:
            await self._abort("ATLAS could not produce a valid plan: " + "; ".join(errors or ["no plan"]))
            return None
        rationale = str(data.get("rationale") or "").strip()
        await self.store.log(
            f"ATLAS planned {len(tasks)} tasks" + (f" · {_short(rationale, 140)}" if rationale else ""),
            mission_id=self.mission_id, agent_id=self.atlas,
        )
        return tasks

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
            if self.limited:  # the plan's usage limit was hit earlier: don't even start
                await self._fail_unstarted(task_id)
                return
            self._active.setdefault(agent_id, []).append(task_id)
            token = current_agent.set(agent_id)
            try:
                await self._attempts(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, UsageLimitError):
                    await self._on_limit(str(exc))
                else:
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
                current_agent.reset(token)
                self._active[agent_id].remove(task_id)
            others = self._active.get(agent_id) or []
            if others:
                other = s.task(others[0])
                await self.scope.set_agent(agent_id, AgentStatus.WORKING, activity=f"Working on '{other.title}'",
                                           task_id=other.id)
            else:
                await self.scope.set_agent(agent_id, AgentStatus.COMPLETED,
                                           activity=f"Delivered '{task.title}'", task_id=task_id)

    async def _attempts(self, task: Task) -> None:
        """Run a task; a transient error (API/network/timeout/external agent) re-runs it up to
        `config.task_retries` times with exponential backoff. The last error propagates."""
        s = self.store
        for attempt in range(self.config.task_retries + 1):
            try:
                await self._run_agent(s.task(task.id))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                done = s.task(task.id).status == TaskStatus.COMPLETED.value
                if (done or self.limited or attempt >= self.config.task_retries or not _retryable(exc)):
                    raise
                delay = self.config.retry_delay * (2 ** attempt)
                agent_id = task.assigned_to or ""
                await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, retries=attempt + 1)
                await s.log(
                    f"{self.scope.name(agent_id)} hit an error on '{task.title}' · retrying in {delay:g}s "
                    f"({attempt + 1}/{self.config.task_retries}) · {_short(str(exc), 140)}",
                    mission_id=self.mission_id, agent_id=agent_id,
                )
                await self.scope.set_agent(agent_id, AgentStatus.WAITING,
                                           activity=_short(f"Retrying '{task.title}' after an error", 120),
                                           task_id=task.id)
                await asyncio.sleep(delay)

    async def _run_agent(self, task: Task) -> None:
        """One attempt at a task: native agents through the backend's executor, external agents (http / cli
        adapters) through atlas.live.external."""
        deps = dependency_inputs(self.scope, task, self.refs)
        agent = self.scope.agents.get(task.assigned_to or "")
        if agent is not None and agent.agent.adapter in EXTERNAL_ADAPTERS:
            from .external import run_external

            await run_external(self.scope, self._briefed(task), deps)
            return
        await self.executor.run_task(self.scope, self._briefed(task), deps)

    async def _on_limit(self, reason: str) -> None:
        if self.limited:
            return
        self.limited = reason
        await self.store.log(
            f"{reason} · ATLAS stops starting new work and will close the mission with what it has",
            mission_id=self.mission_id, agent_id=self.atlas,
        )

    async def _fail_unstarted(self, task_id: str) -> None:
        s = self.store
        task = s.task(task_id)
        agent_id = task.assigned_to or ""
        await s.update_task(task_id, status=TaskStatus.FAILED)
        self.failures.append(f"Task '{task.title}' ({self.scope.name(agent_id)}) not started: {self.limited}")
        await self.scope.set_agent(agent_id, AgentStatus.ERROR, activity=_short(f"Not started: {self.limited}", 140),
                                   task_id=task_id)
        await s.log(f"'{task.title}' not started · {self.limited}", mission_id=self.mission_id, agent_id=agent_id)

    # -- audit (docs/AUDITOR.md) ----------------------------------------------

    def _unaudited(self) -> list[AgentReport]:
        done = {a.agent_report_id for a in self.store.audits_for(self.mission_id)}
        return [r for r in self.store.reports_for(self.mission_id)
                if r.id not in done and self.store.task(r.task_id).round == self.round]

    async def _audit_round(self) -> None:
        """AUDITOR checks this round's reports; each FAIL goes back to its agent for one revision, which is
        audited again (final: no further revision). Never raises: a failed audit is noted in the report."""
        auditor = self.scope.auditor
        reports = self._unaudited()
        if auditor is None or not reports:
            return
        if self.limited:
            self.audit_skipped = self.limited
            await self.store.log(f"Audit skipped · {self.limited}", mission_id=self.mission_id, agent_id=auditor.id)
            return
        if self.store.mission(self.mission_id).phase != MissionPhase.VALIDATION.value:
            await self._phase(MissionPhase.VALIDATION)
        revisions = await self._audit(reports, final=False)
        if not revisions:
            return
        await self._atlas(AgentStatus.REVIEWING, f"Supervising {len(revisions)} revision(s) AUDITOR asked for")
        await self._mark_waiting(revisions)
        await self._execute(revisions)
        revised = [r for r in self._unaudited() if self.store.task(r.task_id).revision_of]
        if revised and not self.limited:
            await self._audit(revised, final=True)

    async def _audit(self, reports: list[AgentReport], *, final: bool) -> list[str]:
        """One audit step; records the audits and returns the ids of the revision tasks it opened."""
        s, auditor = self.store, self.scope.auditor
        assert auditor is not None
        await self.scope.set_agent(auditor.id, AgentStatus.REVIEWING,
                                   activity=f"Auditing {len(reports)} report(s)" + (" (revisions)" if final else ""))
        try:
            audits = await audit_mod.run_audit(self, reports, final=final)
        except LLMError as exc:
            if isinstance(exc, UsageLimitError):
                await self._on_limit(str(exc))
            self.audit_skipped = _short(str(exc), 160)
            await s.log(f"Audit skipped · {_short(str(exc), 160)}", mission_id=self.mission_id, agent_id=auditor.id)
            await self.scope.set_agent(auditor.id, AgentStatus.ERROR, activity=_short(f"Audit failed: {exc}", 120))
            return []
        revisions: list[str] = []
        budget = self.config.audit_revisions
        for audit in audits:
            task = s.task(audit.task_id)
            if (audit.verdict == AuditVerdict.FAIL and not final and budget > 0 and not self.limited
                    and not task.revision_of and task.assigned_to in self.scope.agents):
                revision = await s.create_task(
                    self.mission_id, f"Revise: {task.title}", audit_mod.revision_description(task.description, audit),
                    task.assigned_to, created_by=self.atlas, priority=Priority.HIGH, depends_on=[task.id],
                    revision_of=task.id,
                )
                self.refs[f"{self._ref(task.id)}-rev"] = revision.id
                audit = audit.model_copy(update={"revision_task_id": revision.id})
                revisions.append(revision.id)
            await s.record_audit(audit)
        failed = sum(1 for a in audits if a.verdict == AuditVerdict.FAIL)
        await self.scope.set_agent(
            auditor.id, AgentStatus.COMPLETED,
            activity=f"Audited {len(audits)} report(s)" + (f" · {failed} did not hold up" if failed else ""))
        return revisions

    def _ref(self, task_id: str) -> str:
        return next((k for k, v in self.refs.items() if v == task_id), task_id)

    # -- review + consolidation ---------------------------------------------

    def _tasks_text(self) -> str:
        ref_of = {v: k for k, v in self.refs.items()}
        return "\n".join(
            f"- {ref_of.get(t.id, t.id)} · {t.title} · {self.scope.name(t.assigned_to or '')} · {t.status}"
            for t in self.store.tasks_for(self.mission_id)
        )

    def _reports_text(self) -> list[str]:
        ref_of = {v: k for k, v in self.refs.items()}
        by_report: dict[str, list[Audit]] = {}
        for a in self.store.audits_for(self.mission_id):
            by_report.setdefault(a.agent_report_id, []).append(a)
        out = []
        for r in self.store.reports_for(self.mission_id):
            t = self.store.task(r.task_id)
            text = render_report(r, title=t.title, agent_name=self.scope.name(r.agent_id), ref=ref_of.get(t.id))
            if t.revision_of:
                text += f"\n(Revision of '{self.store.task(t.revision_of).title}' requested by AUDITOR)"
            block = audit_mod.audit_block(by_report.get(r.id, []))
            out.append(text + ("\n" + block if block else ""))
        return out

    async def _review(self) -> list[dict[str, Any]]:
        allowed = set(self.scope.agents)
        if self.limited:
            await self.store.log(f"Review skipped · {self.limited}", mission_id=self.mission_id, agent_id=self.atlas)
            return []
        try:
            data = await self._atlas_step(followups_tool(sorted(allowed)), with_mission_notes(review_message(
                self.scope.objective, self._tasks_text(), self._reports_text(), self.failures,
                [_roster_entry(r) for r in self.roster]), self._notes()))
        except LLMError as exc:
            if isinstance(exc, UsageLimitError):
                await self._on_limit(str(exc))
            await self.store.log(f"Review skipped · {exc}", mission_id=self.mission_id, agent_id=self.atlas)
            return []
        if data is None:
            return []
        assessment = str(data.get("assessment") or "").strip()
        if assessment:
            await self.store.log(f"ATLAS review · {_short(assessment, 200)}", mission_id=self.mission_id,
                                 agent_id=self.atlas)
        tasks, errors = validate_plan(data, allowed, existing_refs=set(self.refs), min_tasks=0,
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
        reports = s.reports_for(self.mission_id)
        fields.setdefault("deliverables", [d for r in reports for d in getattr(r, "deliverables", [])])
        fields.setdefault("audit_summary", audit_mod.audit_summary(
            [a for a in s.audits_for(self.mission_id) if a.round == self.round], skipped=self.audit_skipped))
        return MissionReport(
            mission_id=self.mission_id,
            version=self.round,
            tasks_completed=[t.title for t in tasks if t.status == done],
            tasks_pending=[t.title for t in tasks if t.status != done],
            agent_report_ids=[r.id for r in s.reports_for(self.mission_id)],
            needs_human_attention=attention,
            **fields,
        )

    async def _consolidate(self) -> MissionReport:
        try:
            if self.limited:
                raise LLMError(self.limited)
            prompt = consolidation_message(
                self.scope.objective, self._tasks_text(), self._reports_text(), self.failures)
            if self.store.audits_for(self.mission_id):
                prompt = prompt.replace("Call submit_mission_report now.", AUDIT_CONSOLIDATION_NOTE
                                        + "\n\nCall submit_mission_report now.")
            if self.followup_request is not None:
                prompt = prompt.replace("Call submit_mission_report now.", followup_consolidation_note(
                    self.followup_request, self.round) + "\n\nCall submit_mission_report now.")
            data = await self._atlas_step(SUBMIT_MISSION_REPORT_TOOL, with_mission_notes(prompt, self._notes()))
            if data is None:
                raise LLMError("ATLAS did not call submit_mission_report")
        except LLMError as exc:
            if isinstance(exc, UsageLimitError):
                await self._on_limit(str(exc))
            await self.store.log(f"Consolidation fell back to an automatic summary · {exc}",
                                 mission_id=self.mission_id, agent_id=self.atlas)
            return self._fallback_report(str(exc))
        status = str(data.get("objective_status") or "").upper()
        if status not in ("ACHIEVED", "PARTIAL", "NOT_ACHIEVED"):
            status = "PARTIAL"
        return await self._traced(self._base_report(
            executive_summary=str(data.get("executive_summary") or "").strip() or "(no summary)",
            objective_status=status,
            key_findings=to_claims(data.get("key_findings")),
            conflicts=_str_list(data.get("conflicts")),
            assumptions=_str_list(data.get("assumptions")),
            needs_human_attention=_str_list(data.get("needs_human_attention")),
            next_actions=_str_list(data.get("next_actions")),
            references=_str_list(data.get("references")),
        ))

    async def _traced(self, report: MissionReport) -> MissionReport:
        """System check on the executive report: figures that come from no agent report / evidence / human."""
        untraced = audit_mod.untraced_figures(report, audit_mod.report_corpus(self))
        if untraced:
            await self.store.log(
                f"AUDITOR · {len(untraced)} figure(s) in the executive report trace to no agent report: "
                + "; ".join(u.split(" — ")[0] for u in untraced[:5]),
                mission_id=self.mission_id, agent_id=self.scope.auditor.id if self.scope.auditor else self.atlas,
            )
        return report.model_copy(update={"untraced": untraced})

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

    # -- resume (Phase 5) ---------------------------------------------------------

    def resumable(self) -> list[Task]:
        """Tasks a resume would run again: FAILED or CANCELLED, not superseded by a completed revision."""
        tasks = self.store.tasks_for(self.mission_id)
        revised = {t.revision_of for t in tasks if t.revision_of and t.status == TaskStatus.COMPLETED.value}
        return [t for t in tasks if t.status in _DEAD and t.id not in revised]

    async def run_resume(self) -> None:
        try:
            await self._resume_flow()
        except asyncio.CancelledError:
            await self._cancel_children()
            raise
        except Exception as exc:
            log.exception("resume of mission %s failed", self.mission_id)
            await self._cancel_children()
            try:
                await self._abort(f"Unexpected error while resuming: {exc}")
            except Exception:  # pragma: no cover
                log.exception("could not close the resumed mission %s", self.mission_id)
        await self.store.release_agents(self.mission_id, self.scope.touched)

    async def _resume_flow(self) -> None:
        s = self.store
        again = self.resumable()
        mission = await s.begin_round(self.mission_id)
        self.round = mission.round
        titles = ", ".join(f"'{t.title}'" for t in again[:4]) + (" …" if len(again) > 4 else "")
        await s.log(f"ATLAS resumes the mission · round {self.round} re-runs {len(again)} task(s): {titles}",
                    mission_id=self.mission_id, agent_id=self.atlas)
        # the same tasks run again in this round: reset them (deps first) so the DAG scheduler picks them up
        ids = [t.id for t in again]
        for t in again:
            ready = all(s.task(d).status == TaskStatus.COMPLETED.value for d in t.depends_on)
            await s.reopen_task(t.id, self.round, ready=ready)
        await self._mark_waiting(ids)
        await self._phase(MissionPhase.EXECUTION)
        await self._atlas(AgentStatus.REVIEWING, f"Supervising {len(ids)} resumed task(s)")
        await self._execute(ids)

        await self._audit_round()
        await self._phase(MissionPhase.CONSOLIDATION)
        await self._atlas(AgentStatus.WORKING, f"Consolidating round {self.round}")
        report = await self._consolidate()
        await self._phase(MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        await self._phase(MissionPhase.FOLLOW_UP)
        await self._atlas(AgentStatus.COMPLETED, f"Mission report v{report.version} delivered")
        await self._phase(MissionPhase.CLOSED)

    # -- mission thread (docs/PHASE3.md B) -----------------------------------

    def _notes(self) -> list[str]:
        """Human guidance: every note the human wrote in this mission's thread, oldest first."""
        return [m.body for m in self.store.messages_for(self.mission_id)
                if m.from_agent == HUMAN and m.type == MessageType.REQUEST.value]

    def _attachment_names(self) -> list[str]:
        return [a.name for a in self.store.mission(self.mission_id).attachments]

    def _briefed(self, task: Task) -> Task:
        """The task as the agent sees it: its description plus the attachments and the human guidance so far
        (the stored task is unchanged)."""
        notes, names = self._notes(), self._attachment_names()
        if not notes and not names:
            return task
        return task.model_copy(update={"description": with_mission_notes(task.description, notes, names)})

    def acknowledge(self, message: AgentMessage) -> None:
        """Reply to a note sent while the mission runs (fast model, in the background)."""
        child = asyncio.create_task(self._acknowledge(message), name=f"live-ack:{message.id}")
        self._children.add(child)
        child.add_done_callback(self._children.discard)

    async def _acknowledge(self, message: AgentMessage) -> None:
        text = ACK_FALLBACK
        if not self.limited:
            try:
                data = await self.executor.structured(
                    self.scope, model=self.config.models.fast, system=self._system(),
                    prompt=acknowledge_message(self.scope.objective, message.body,
                                               self.store.mission(self.mission_id).phase),
                    tool=ACKNOWLEDGE_TOOL, max_tokens=400,
                )
                text = str((data or {}).get("text") or "").strip() or ACK_FALLBACK
            except LLMError as exc:
                log.info("acknowledgement fell back: %s", exc)
        await self.store.send_message(self.mission_id, self.atlas, HUMAN, MessageType.ANSWER,
                                      "Re: " + _short(message.body, 70), text, in_reply_to=message.id)

    async def run_followup(self, message: AgentMessage) -> None:
        """A follow-up on a closed or interrupted mission: answer and/or run one more round."""
        try:
            await self._followup_flow(message)
        except asyncio.CancelledError:
            await self._cancel_children()
            raise
        except Exception as exc:
            log.exception("follow-up of mission %s failed", self.mission_id)
            await self._cancel_children()
            try:
                if self.store.mission(self.mission_id).phase == MissionPhase.CLOSED.value:
                    await self._reply(message, f"I could not process the follow-up: {exc}")
                else:
                    await self._abort(f"Unexpected error in round {self.round}: {exc}")
            except Exception:  # pragma: no cover
                log.exception("could not close the follow-up of mission %s", self.mission_id)
        await self.store.release_agents(self.mission_id, self.scope.touched)

    async def _reply(self, message: AgentMessage, text: str, kind: MessageType = MessageType.ANSWER) -> None:
        await self.store.send_message(self.mission_id, self.atlas, HUMAN, kind, "Re: " + _short(message.body, 70),
                                      text, in_reply_to=message.id)

    def _followup_tasks_text(self) -> str:
        ref_of = {v: k for k, v in self.refs.items()}
        return "\n".join(
            f"- {ref_of.get(t.id, t.id)} · {t.title} · {self.scope.name(t.assigned_to or '')} · {t.status} · "
            f"round {t.round}"
            for t in self.store.tasks_for(self.mission_id)
        )

    def _thread_text(self, exclude: str) -> list[str]:
        out = []
        for m in self.store.messages_for(self.mission_id):
            if m.id == exclude or HUMAN not in (m.from_agent, m.to_agent):
                continue
            who = "Human" if m.from_agent == HUMAN else self.scope.name(m.from_agent)
            out.append(f"[{who}] {_short(m.body, 600)}")
        return out

    async def _followup_flow(self, message: AgentMessage) -> None:
        s = self.store
        mission = s.mission(self.mission_id)
        allowed = set(self.scope.agents)
        await self._atlas(AgentStatus.WORKING, "Reading your follow-up")
        reports = s.mission_reports_for(self.mission_id)
        prompt = followup_message(
            self.scope.objective, self.scope.node, request=message.body, round_no=mission.round,
            interrupted=mission.interrupted,
            latest_report=render_mission_report(reports[-1]) if reports else "",
            tasks_text=self._followup_tasks_text(), reports=self._reports_text(),
            thread=self._thread_text(exclude=message.id), attachments=self._attachment_names(),
            roster=[_roster_entry(r) for r in self.roster],
        )
        tasks: list[dict[str, Any]] = []
        errors: list[str] = []

        async def validate(data: dict[str, Any] | None) -> list[str]:
            nonlocal tasks, errors
            if data is None:
                tasks, errors = [], ["you must call respond_to_followup"]
            elif not data.get("tasks"):
                tasks = []
                errors = [] if str(data.get("answer") or "").strip() else ["give an answer, tasks, or both"]
            else:
                tasks, errors = validate_plan(data, allowed, existing_refs=set(self.refs), min_tasks=0,
                                              max_tasks=MAX_ROUND_TASKS)
            return errors

        try:
            data = await self._atlas_step(respond_to_followup_tool(sorted(allowed)), prompt,
                                          validate=validate, attempts=2)
        except LLMError as exc:
            await self._reply(message, f"I could not process the follow-up: {exc}")
            await self._atlas(AgentStatus.ERROR, _short(f"Follow-up failed: {exc}", 120))
            return
        if data is None or errors:
            await self._reply(message, "I could not turn this follow-up into valid work: "
                              + "; ".join(errors or ["no response"]))
            await self._atlas(AgentStatus.COMPLETED, "Follow-up not processed")
            return
        answer = str(data.get("answer") or "").strip()
        if answer:
            await self._reply(message, answer)
        if not tasks:
            await self._atlas(AgentStatus.COMPLETED, "Answered your follow-up")
            return

        self.followup_request = message.body
        mission = await s.begin_round(self.mission_id)
        self.round = mission.round
        await s.log(f"ATLAS opened round {self.round} with {len(tasks)} task(s)", mission_id=self.mission_id,
                    agent_id=self.atlas)
        await self._atlas(AgentStatus.WORKING, f"Delegating {len(tasks)} tasks (round {self.round})")
        ids = await self._create_tasks(tasks)
        await self._mark_waiting(ids)

        await self._phase(MissionPhase.EXECUTION)
        await self._atlas(AgentStatus.REVIEWING, f"Supervising {len(ids)} tasks (round {self.round})")
        await self._execute(ids)

        await self._phase(MissionPhase.VALIDATION)
        await self._audit_round()
        await self._phase(MissionPhase.CONSOLIDATION)
        await self._atlas(AgentStatus.WORKING, f"Consolidating round {self.round}")
        report = await self._consolidate()

        await self._phase(MissionPhase.REPORTING)
        await self._atlas(AgentStatus.WORKING, f"Writing executive report v{report.version}")
        await s.submit_mission_report(report)
        await self._phase(MissionPhase.FOLLOW_UP)
        await self._reply(message, f"Round {self.round} complete · report v{report.version} "
                          f"({report.objective_status}): {_short(report.executive_summary, 400)}",
                          MessageType.RESULT)
        await self._atlas(AgentStatus.COMPLETED, f"Mission report v{report.version} delivered")
        await self._phase(MissionPhase.CLOSED)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LiveEngine:
    """Starts, tracks and cancels live missions. Concurrency caps are shared by all missions.

    The backend (api | subscription) is picked per mission: `backend=` if given, else ATLAS_LLM_BACKEND
    (see backend.py). An explicit `config`/`loader` applies to every backend (tests); otherwise each
    backend gets its own env-derived config (models and web-search defaults differ).
    """

    def __init__(
        self,
        store: WorldStore,
        *,
        loader: AgentLoader | None = None,
        context: NodeContext | None = None,
        config: LiveConfig | None = None,
        llm: LLMClient | None = None,
        prices: PriceTable | None = None,
        backend: str | None = None,
        sdk_query: Any = None,
    ):
        self.store = store
        self._explicit_config = config
        self._explicit_loader = loader
        self.config = config or LiveConfig.from_env()
        self.loader = loader or AgentLoader(store.registry, self.config.models)
        self.context = context or NodeContext(store.registry)
        self.prices = prices or PriceTable.from_env()
        self._llm = llm
        self.backend = backend or ("api" if llm is not None else None)  # an injected Messages client means api
        self._sdk_query = sdk_query
        self._configs: dict[str, LiveConfig] = {}
        self._loaders: dict[str, AgentLoader] = {}
        self._executors: dict[str, Executor] = {}
        self.global_slot = asyncio.Semaphore(self.config.max_concurrency)
        self._agent_slots: dict[str, asyncio.Semaphore] = {}
        self._running: dict[str, tuple[LiveMission, asyncio.Task[None]]] = {}

    # -- backends ------------------------------------------------------------

    def backend_info(self) -> BackendInfo:
        if self.backend:
            return BackendInfo(self.backend)
        return detect_backend()

    def config_for(self, backend: str) -> LiveConfig:
        if self._explicit_config is not None:
            return self._explicit_config
        if backend not in self._configs:
            self._configs[backend] = self.config if backend == "api" else LiveConfig.from_env(backend)
        return self._configs[backend]

    def loader_for(self, backend: str) -> AgentLoader:
        if self._explicit_loader is not None:
            return self._explicit_loader
        if backend not in self._loaders:
            self._loaders[backend] = (
                self.loader if backend == "api" else AgentLoader(self.store.registry, self.config_for(backend).models)
            )
        return self._loaders[backend]

    def executor(self, backend: str) -> Executor:
        if backend not in self._executors:
            if backend == "subscription":
                from .sdk import SubscriptionExecutor

                self._executors[backend] = SubscriptionExecutor(query=self._sdk_query)
            else:
                self._executors[backend] = ApiExecutor()
        return self._executors[backend]

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

    async def start(self, objective: str, node: str, backend: str | None = None, *,
                    mission_id: str | None = None, attachments: list[Attachment] | None = None) -> Mission:
        chosen = backend or self.backend_info().backend or "api"
        mission = await self.store.create_mission(objective, node, mode="live", mission_id=mission_id,
                                                  attachments=attachments)
        live = LiveMission(self, mission, chosen)
        await self.store.log(
            f"Live mission on {'your Claude plan (Claude Code)' if chosen == 'subscription' else 'the Claude API'}",
            mission_id=mission.id, agent_id=self.store.registry.orchestrator.id,
        )
        task = asyncio.create_task(live.run(), name=f"live-mission:{mission.id}")
        self._running[mission.id] = (live, task)
        task.add_done_callback(lambda _t, mid=mission.id: self._running.pop(mid, None))
        return mission

    async def post_message(self, mission_id: str, text: str, backend: str | None = None) -> AgentMessage:
        """The human writes in a live mission's thread (docs/PHASE3.md B). Running: guidance + acknowledgement.
        Closed or interrupted: a follow-up (answer and/or a new round) on `backend`."""
        s = self.store
        mission = s.mission(mission_id)
        if mission.mode != "live":
            raise ValueError("follow-ups need a live mission")
        atlas = s.registry.orchestrator.id
        message = await s.send_message(mission_id, HUMAN, atlas, MessageType.REQUEST, _short(text, 80), text)
        live = self.mission(mission_id)
        if live is not None:
            await s.log("Human guidance added · it applies to the work that starts from now on",
                        mission_id=mission_id, agent_id=atlas)
            live.acknowledge(message)
            return message
        chosen = backend or self.backend_info().backend or "api"
        live = LiveMission(self, s.mission(mission_id), chosen)
        task = asyncio.create_task(live.run_followup(message), name=f"live-followup:{mission_id}")
        self._running[mission_id] = (live, task)
        task.add_done_callback(lambda _t, mid=mission_id: self._running.pop(mid, None))
        return message

    async def resume(self, mission_id: str, backend: str | None = None) -> Mission:
        """Re-run a closed live mission's failed and cancelled tasks as a new round (new report version).
        Raises ValueError when there is nothing to resume or the mission is still running."""
        s = self.store
        mission = s.mission(mission_id)
        if mission.mode != "live":
            raise ValueError("only live missions can be resumed")
        if mission_id in self._running or mission.phase != MissionPhase.CLOSED.value:
            raise ValueError("the mission is still running")
        chosen = backend or self.backend_info().backend or "api"
        live = LiveMission(self, mission, chosen)
        if not live.resumable():
            raise ValueError("nothing to resume: no failed or cancelled tasks")
        task = asyncio.create_task(live.run_resume(), name=f"live-resume:{mission_id}")
        self._running[mission_id] = (live, task)
        task.add_done_callback(lambda _t, mid=mission_id: self._running.pop(mid, None))
        return s.mission(mission_id)

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

    async def stop_all(self) -> None:
        """Server shutdown: stop every running mission WITHOUT closing it, so the next start restores it
        as interrupted (store.recover_interrupted)."""
        entries = list(self._running.values())
        for _, task in entries:
            task.cancel()
        for _, task in entries:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover
                log.exception("live mission raised while stopping")
        self._running.clear()

    async def cancel_all(self) -> None:
        for mid in list(self._running):
            try:
                await self.cancel(mid, "ATLAS is shutting down")
            except Exception:  # pragma: no cover - best effort at shutdown/reset
                log.exception("could not cancel live mission %s", mid)
        self._running.clear()
