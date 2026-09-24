"""Mission simulator (Phase 1).

`MissionRunner` plays a `Scenario` through the real `WorldStore` as an asyncio task, so the
Command Center cannot tell a simulated mission from a live one. `Simulator` owns the running
missions (several may run concurrently) and `ScenarioLibrary` loads scenarios from disk.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from ..core.models import (
    AgentStatus,
    ApprovalState,
    Mission,
    MissionReport,
    TaskStatus,
)
from ..core.registry import AgentRegistry
from ..core.store import NotFoundError, WorldStore
from .scenario import (
    SCENARIOS_DIR,
    AgentStep,
    ApprovalStep,
    LogStep,
    MessageStep,
    MissionReportStep,
    PhaseStep,
    ReportStep,
    Scenario,
    ScenarioError,
    TaskStep,
    TaskUpdateStep,
    validate_scenario,
)

log = logging.getLogger("atlas.sim")


class ScenarioLibrary:
    """Scenarios from `directory` (re-read on every call, so new files show up without a
    restart) plus scenarios registered in code (tests). A broken file is skipped and
    reported in `errors` instead of taking the server down."""

    def __init__(self, registry: AgentRegistry, directory: Path | None = SCENARIOS_DIR):
        self.registry = registry
        self.directory = directory
        self._extra: dict[str, Scenario] = {}
        self.errors: list[str] = []

    def add(self, scenario: Scenario) -> Scenario:
        validate_scenario(scenario, self.registry)
        self._extra[scenario.id] = scenario
        return scenario

    def all(self) -> dict[str, Scenario]:
        found: dict[str, Scenario] = {}
        errors: list[str] = []
        if self.directory is not None and self.directory.is_dir():
            for path in sorted(self.directory.glob("*.y*ml")):
                try:
                    sc = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
                    validate_scenario(sc, self.registry)
                    if sc.id in found:
                        raise ScenarioError(f"duplicate scenario id '{sc.id}'")
                    found[sc.id] = sc
                except (ValueError, yaml.YAMLError, OSError) as exc:  # a bad file must not break the API
                    errors.append(f"{path.name}: {exc}")
        for msg in errors:
            if msg not in self.errors:
                log.warning("skipping scenario %s", msg)
        self.errors = errors
        found.update(self._extra)
        return found

    def get(self, scenario_id: str) -> Scenario | None:
        return self.all().get(scenario_id)

    def default_for(self, node: str) -> Scenario | None:
        return next((sc for sc in self.all().values() if sc.node == node), None)


class MissionRunner:
    def __init__(self, store: WorldStore, scenario: Scenario, mission: Mission, speed: float = 1.0):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.store = store
        self.scenario = scenario
        self.mission_id = mission.id
        self.speed = speed
        self.refs: dict[str, str] = {}  # scenario task ref -> real task id
        self.approvals: dict[str, str] = {}  # scenario approval ref -> approval id
        self.touched: set[str] = set()
        self.waiting_on: str | None = None  # approval id while paused

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._play(self.scenario.steps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface in the feed instead of dying silently
            log.exception("simulation %s failed", self.scenario.id)
            await self.store.log(
                f"Simulation error: {exc}", mission_id=self.mission_id, agent_id=self._orch
            )
        await self._release_agents()

    async def _release_agents(self) -> None:
        active_elsewhere = {
            t.id
            for m in self.store.snapshot().missions
            if m.id != self.mission_id and m.phase != "CLOSED"
            for t in self.store.tasks_for(m.id)
        }
        for agent_id in sorted(self.touched):
            st = self.store.agent_state(agent_id)
            if st.current_task_id and st.current_task_id in active_elsewhere:
                continue  # now busy on another running mission
            if st.status == self.store.default_status(agent_id).value and not st.current_task_id:
                continue
            await self.store.reset_agent(agent_id, mission_id=self.mission_id)

    # -- steps ---------------------------------------------------------------

    async def _play(self, steps: list) -> None:
        for step in steps:
            await asyncio.sleep(step.wait / self.speed)
            await self._step(step)

    def _task(self, ref: str | None) -> str | None:
        return None if ref is None else self.refs[ref]

    @property
    def _orch(self) -> str:
        return self.store.registry.orchestrator.id

    async def _set_agent(self, agent_id: str, status, **kw) -> None:
        self.touched.add(agent_id)
        await self.store.set_agent_state(agent_id, status, mission_id=self.mission_id, **kw)

    async def _step(self, st) -> None:
        s = self.store
        mid = self.mission_id
        if isinstance(st, PhaseStep):
            await s.set_phase(mid, st.phase)
        elif isinstance(st, TaskStep):
            task = await s.create_task(
                mid,
                st.title,
                st.description,
                st.assigned_to,
                priority=st.priority,
                depends_on=[self.refs[r] for r in st.depends_on],
                requires_approval=st.requires_approval,
                approval_reason=st.approval_reason,
            )
            self.refs[st.ref] = task.id
        elif isinstance(st, TaskUpdateStep):
            await s.update_task(self.refs[st.ref], status=st.status, progress=st.progress)
        elif isinstance(st, AgentStep):
            self.touched.update(st.with_)
            await self._set_agent(
                st.agent,
                st.status,
                activity=st.activity,
                task_id=self._task(st.task),
                collaborating_with=st.with_,
            )
        elif isinstance(st, MessageStep):
            await s.send_message(
                mid,
                st.from_,
                st.to,
                st.type,
                st.subject,
                st.body,
                task_id=self._task(st.task),
                confidence=st.confidence,
                requires_response=st.requires_response,
            )
        elif isinstance(st, ReportStep):
            await s.submit_report(
                mid,
                self.refs[st.task],
                st.agent,
                st.asked_to,
                actions_taken=st.actions_taken,
                inputs_used=st.inputs_used,
                findings=st.findings,
                unresolved=st.unresolved,
                needs_agents=st.needs_agents,
                confidence=st.confidence,
                limitations=st.limitations,
            )
        elif isinstance(st, ApprovalStep):
            await self._approval(st)
        elif isinstance(st, MissionReportStep):
            tasks = s.tasks_for(mid)
            done = TaskStatus.COMPLETED.value
            report = MissionReport(
                mission_id=mid,
                executive_summary=st.executive_summary,
                objective_status=st.objective_status,
                tasks_completed=[t.title for t in tasks if t.status == done],
                tasks_pending=[t.title for t in tasks if t.status != done],
                key_findings=st.key_findings,
                conflicts=st.conflicts,
                assumptions=st.assumptions,
                needs_human_attention=st.needs_human_attention,
                next_actions=st.next_actions,
                references=st.references,
                agent_report_ids=[r.id for r in s.reports_for(mid)],
            )
            await s.submit_mission_report(report)
        elif isinstance(st, LogStep):
            await s.log(st.text, mission_id=mid, agent_id=st.agent)
        else:  # pragma: no cover - the schema is a closed union
            raise TypeError(f"unknown step {st!r}")

    async def _approval(self, st: ApprovalStep) -> None:
        s = self.store
        task_id = self._task(st.task)
        approval = await s.request_approval(
            self.mission_id,
            st.requested_by,
            st.reason,
            st.title,
            st.detail,
            proposed_action=st.proposed_action,
            task_id=task_id,
        )
        self.approvals[st.ref] = approval.id
        if task_id:
            await s.update_task(task_id, status=TaskStatus.AWAITING_APPROVAL)
        await self._set_agent(
            st.requested_by, AgentStatus.WAITING, activity="Awaiting human approval", task_id=task_id
        )
        self.waiting_on = approval.id
        try:
            decided = await s.wait_for_decision(approval.id)
        finally:
            self.waiting_on = None
        if decided.state == ApprovalState.APPROVED.value:
            if task_id:
                await s.update_task(task_id, status=TaskStatus.IN_PROGRESS)
            await self._set_agent(
                st.requested_by,
                AgentStatus.WORKING,
                activity=f"Approved · {st.proposed_action or st.title}",
                task_id=task_id,
            )
        else:
            if task_id:
                await s.update_task(task_id, status=TaskStatus.BLOCKED)
            await self._set_agent(
                st.requested_by,
                s.default_status(st.requested_by),
                activity=f"Rejected · {st.title}",
                task_id=task_id,
            )
            await self._play(st.on_reject)


class Simulator:
    """Starts and tracks concurrently running simulated missions."""

    def __init__(self, store: WorldStore, library: ScenarioLibrary, default_speed: float = 1.0):
        self.store = store
        self.library = library
        self.default_speed = default_speed
        self._running: dict[str, tuple[MissionRunner, asyncio.Task[None]]] = {}

    async def start(
        self,
        scenario: Scenario,
        *,
        objective: str | None = None,
        node: str | None = None,
        speed: float | None = None,
    ) -> Mission:
        node = node or scenario.node
        if node != scenario.node:
            raise ValueError(f"scenario '{scenario.id}' belongs to node '{scenario.node}', not '{node}'")
        speed = speed if speed is not None else self.default_speed
        if speed <= 0:
            raise ValueError("speed must be > 0")
        mission = await self.store.create_mission(objective or scenario.objective, node)
        runner = MissionRunner(self.store, scenario, mission, speed)
        task = asyncio.create_task(runner.run(), name=f"mission:{mission.id}")
        self._running[mission.id] = (runner, task)
        task.add_done_callback(lambda _t, mid=mission.id: self._running.pop(mid, None))
        return mission

    def runner(self, mission_id: str) -> MissionRunner | None:
        entry = self._running.get(mission_id)
        return entry[0] if entry else None

    def running(self) -> list[str]:
        return list(self._running)

    async def wait(self, mission_id: str) -> None:
        entry = self._running.get(mission_id)
        if entry:
            await asyncio.shield(entry[1])

    async def cancel_all(self) -> None:
        tasks = [t for _, t in self._running.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, NotFoundError):
                pass
        self._running.clear()

    async def reset(self) -> None:
        await self.cancel_all()
        await self.store.reset()
