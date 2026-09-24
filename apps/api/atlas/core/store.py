"""WorldStore — the single, authoritative in-memory state of ATLAS.

Every mutation goes through an async method that (1) updates the state and (2) publishes one
`AtlasEvent` whose payload carries the *full* updated object(s) (see docs/EVENTS.md).
`apply_event` is the pure reducer that the Command Center mirrors: folding the event stream
over the initial state reproduces `WorldStore.snapshot()`.

Payload keys (primary key per docs/EVENTS.md, plus secondary objects touched by the same
mutation so clients never have to derive anything):

    mission.created / phase_changed / closed   {mission}
    mission.updated                            {mission}         usage/cost changed (live mode)
    task.created                               {task, mission}   mission.task_ids gained the task
    task.updated                               {task}
    agent.state_changed                        {state}
    message.sent                               {message}
    report.submitted                           {report, task?}   task.result_report_id was set
    mission.report_ready                       {report, mission} mission.final_report_id was set
    approval.requested / approval.decided      {approval}
    log                                        {}                or {reset: true, agent_states: [...]}
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from .events import EventBus
from .models import (
    AgentMessage,
    AgentReport,
    AgentState,
    AgentStatus,
    ApprovalReason,
    ApprovalRequest,
    ApprovalState,
    AtlasEvent,
    Claim,
    Confidence,
    EventType,
    MessageType,
    Mission,
    MissionPhase,
    MissionReport,
    Priority,
    Task,
    TaskStatus,
    Usage,
    WorldState,
    _now,
)
from .registry import AgentRegistry, RegistryError

MONITORING_CAPABILITY = "monitoring"
_TERMINAL = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
_NOT_STARTED = {TaskStatus.PENDING.value, TaskStatus.READY.value}


class StoreError(ValueError):
    """Invalid mutation (bad transition, bad reference)."""


class NotFoundError(StoreError):
    pass


class IsolationError(StoreError):
    """An agent was asked to work on a mission outside its node."""


class ConflictError(StoreError):
    """E.g. deciding an approval that is no longer pending."""


# ---------------------------------------------------------------------------
# Reducer (the spec the frontend mirrors)
# ---------------------------------------------------------------------------

# payload key -> (WorldState collection, model, identity field)
_COLLECTIONS: dict[str, tuple[str, type[BaseModel], str]] = {
    "mission": ("missions", Mission, "id"),
    "task": ("tasks", Task, "id"),
    "state": ("agent_states", AgentState, "agent_id"),
    "message": ("messages", AgentMessage, "id"),
    "approval": ("approvals", ApprovalRequest, "id"),
}


def _upsert(items: list[Any], obj: Any, key: str = "id") -> None:
    ident = getattr(obj, key)
    for i, existing in enumerate(items):
        if getattr(existing, key) == ident:
            items[i] = obj
            return
    items.append(obj)


def _clear(state: WorldState) -> None:
    state.missions = []
    state.tasks = []
    state.messages = []
    state.agent_reports = []
    state.mission_reports = []
    state.approvals = []


def apply_event(state: WorldState, event: AtlasEvent) -> WorldState:
    """Pure reducer: returns a new WorldState with `event` applied.

    Rules: for every known payload key, upsert the full object into its collection by id
    (`agent_id` for agent states). `report` goes to `mission_reports` for
    `mission.report_ready`, otherwise to `agent_reports`. A `log` event with
    `payload.reset` clears missions/tasks/messages/reports/approvals and replaces
    `agent_states`. `last_seq` becomes the event's seq.
    """
    s = state.model_copy(deep=True)
    p = event.payload or {}
    etype = EventType(event.type)

    if etype == EventType.LOG and p.get("reset"):
        _clear(s)
        s.agent_states = [AgentState.model_validate(x) for x in p.get("agent_states", [])]

    for key, value in p.items():
        if value is None:
            continue
        if key == "report":
            if etype == EventType.MISSION_REPORT_READY:
                _upsert(s.mission_reports, MissionReport.model_validate(value))
            else:
                _upsert(s.agent_reports, AgentReport.model_validate(value))
        elif key in _COLLECTIONS:
            collection, model, ident = _COLLECTIONS[key]
            _upsert(getattr(s, collection), model.model_validate(value), ident)

    s.last_seq = max(s.last_seq, event.seq)
    return s


def fold(state: WorldState, events: Iterable[AtlasEvent]) -> WorldState:
    for e in events:
        state = apply_event(state, e)
    return state


def _dump(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json", by_alias=True)


def _enum_val(v: Any) -> Any:
    return getattr(v, "value", v)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class WorldStore:
    def __init__(self, registry: AgentRegistry, bus: EventBus):
        self.registry = registry
        self.bus = bus
        self._state = WorldState(
            nodes=registry.nodes(),
            divisions=registry.divisions(),
            agents=registry.all(),
            agent_states=self._initial_agent_states(),
        )
        self._decisions: dict[str, asyncio.Event] = {}

    # -- queries -------------------------------------------------------------

    def snapshot(self) -> WorldState:
        s = self._state.model_copy(deep=True)
        s.last_seq = self.bus.last_seq
        return s

    def default_status(self, agent_id: str) -> AgentStatus:
        agent = self.registry.get(agent_id)
        return AgentStatus.MONITORING if MONITORING_CAPABILITY in agent.capabilities else AgentStatus.IDLE

    def mission(self, mission_id: str) -> Mission:
        return self._find(self._state.missions, mission_id, "mission")

    def task(self, task_id: str) -> Task:
        return self._find(self._state.tasks, task_id, "task")

    def approval(self, approval_id: str) -> ApprovalRequest:
        return self._find(self._state.approvals, approval_id, "approval")

    def agent_state(self, agent_id: str) -> AgentState:
        for st in self._state.agent_states:
            if st.agent_id == agent_id:
                return st
        raise NotFoundError(f"unknown agent '{agent_id}'")

    def tasks_for(self, mission_id: str) -> list[Task]:
        return [t for t in self._state.tasks if t.mission_id == mission_id]

    def reports_for(self, mission_id: str) -> list[AgentReport]:
        return [r for r in self._state.agent_reports if r.mission_id == mission_id]

    # -- missions ------------------------------------------------------------

    async def create_mission(
        self,
        objective: str,
        node: str,
        *,
        context: str | None = None,
        priority: Priority | str = Priority.MEDIUM,
        mode: str = "simulated",
    ) -> Mission:
        self._check_node(node)
        mission = Mission(objective=objective, node=node, context=context, priority=priority, mode=mode)
        self._state.missions.append(mission)
        node_name = self._node_name(node)
        await self._emit(
            EventType.MISSION_CREATED,
            f"New {node_name} mission · {objective}",
            {"mission": mission},
            mission_id=mission.id,
            agent_id=self.registry.orchestrator.id,
        )
        return mission

    async def set_phase(self, mission_id: str, phase: MissionPhase | str) -> Mission:
        mission = self.mission(mission_id)
        phase = MissionPhase(phase)
        if mission.phase == MissionPhase.CLOSED.value:
            raise StoreError(f"mission '{mission_id}' is closed")
        changes: dict[str, Any] = {"phase": phase.value}
        if phase == MissionPhase.CLOSED:
            changes["closed_at"] = _now()
        mission = self._replace(self._state.missions, mission, changes)
        closed = phase == MissionPhase.CLOSED
        await self._emit(
            EventType.MISSION_CLOSED if closed else EventType.MISSION_PHASE_CHANGED,
            "Mission closed" if closed else f"Mission phase → {phase.value}",
            {"mission": mission},
            mission_id=mission.id,
            agent_id=self.registry.orchestrator.id,
        )
        return mission

    async def update_usage(self, mission_id: str, delta: Usage) -> Mission:
        """Add one LLM call's usage (tokens + estimated cost) to the mission; emits mission.updated."""
        mission = self.mission(mission_id)
        u = mission.usage
        usage = Usage(
            input_tokens=u.input_tokens + delta.input_tokens,
            output_tokens=u.output_tokens + delta.output_tokens,
            cache_read_tokens=u.cache_read_tokens + delta.cache_read_tokens,
            llm_calls=u.llm_calls + delta.llm_calls,
            est_cost_usd=round(u.est_cost_usd + delta.est_cost_usd, 6),
        )
        mission = self._replace(self._state.missions, mission, {"usage": usage.model_dump()})
        tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens
        await self._emit(
            EventType.MISSION_UPDATED,
            f"Usage · {usage.llm_calls} LLM calls · {tokens:,} tokens · ~${usage.est_cost_usd:.4f}",
            {"mission": mission},
            mission_id=mission.id,
            agent_id=self.registry.orchestrator.id,
        )
        return mission

    async def cancel_mission(self, mission_id: str, reason: str = "cancelled by the user") -> Mission:
        """Cancel every open task, expire pending approvals (waking their waiters) and close the
        mission. Stopping the engine that drives the mission is the caller's job."""
        mission = self.mission(mission_id)
        if mission.phase == MissionPhase.CLOSED.value:
            raise ConflictError(f"mission '{mission_id}' is already closed")
        for t in self.tasks_for(mission_id):
            if t.status not in _TERMINAL:
                await self.update_task(t.id, status=TaskStatus.CANCELLED)
        for a in list(self._state.approvals):
            if a.mission_id == mission_id and a.state == ApprovalState.PENDING.value:
                a = self._replace(
                    self._state.approvals,
                    a,
                    {"state": ApprovalState.EXPIRED.value, "decision_note": reason, "decided_at": _now()},
                )
                await self._emit(
                    EventType.APPROVAL_DECIDED,
                    f"Approval '{a.title}' expired · mission cancelled",
                    {"approval": a},
                    mission_id=mission_id,
                    agent_id=a.requested_by,
                )
                if ev := self._decisions.get(a.id):
                    ev.set()
        await self.log(f"Mission cancelled · {reason}", mission_id=mission_id,
                       agent_id=self.registry.orchestrator.id)
        return await self.set_phase(mission_id, MissionPhase.CLOSED)

    async def release_agents(self, mission_id: str, agent_ids: Iterable[str]) -> None:
        """Return agents touched by a finished mission to their default status, unless they are
        now busy on a task of another open mission."""
        active_elsewhere = {
            t.id
            for m in self._state.missions
            if m.id != mission_id and m.phase != MissionPhase.CLOSED.value
            for t in self.tasks_for(m.id)
        }
        for agent_id in sorted(set(agent_ids)):
            st = self.agent_state(agent_id)
            if st.current_task_id and st.current_task_id in active_elsewhere:
                continue
            if st.status == self.default_status(agent_id).value and not st.current_task_id:
                continue
            await self.reset_agent(agent_id, mission_id=mission_id)

    # -- tasks ---------------------------------------------------------------

    async def create_task(
        self,
        mission_id: str,
        title: str,
        description: str,
        assigned_to: str | None,
        *,
        created_by: str | None = None,
        priority: Priority | str = Priority.MEDIUM,
        depends_on: list[str] | None = None,
        requires_approval: bool = False,
        approval_reason: ApprovalReason | str | None = None,
        parent_task_id: str | None = None,
    ) -> Task:
        mission = self._open_mission(mission_id)
        created_by = created_by or self.registry.orchestrator.id
        self._check_agent(created_by, mission)
        if assigned_to:
            self._check_agent(assigned_to, mission)
        deps = list(depends_on or [])
        for dep in deps:
            if self.task(dep).mission_id != mission_id:
                raise StoreError(f"dependency '{dep}' belongs to another mission")
        status = TaskStatus.READY if self._deps_met(deps) else TaskStatus.PENDING
        task = Task(
            mission_id=mission_id,
            title=title,
            description=description,
            assigned_to=assigned_to,
            created_by=created_by,
            status=status,
            priority=priority,
            depends_on=deps,
            requires_approval=requires_approval,
            approval_reason=approval_reason,
            parent_task_id=parent_task_id,
        )
        self._state.tasks.append(task)
        mission = self._replace(self._state.missions, mission, {"task_ids": [*mission.task_ids, task.id]})
        who = self._name(created_by)
        summary = (
            f"{who} assigned '{title}' to {self._name(assigned_to)}"
            if assigned_to
            else f"{who} created task '{title}'"
        )
        await self._emit(
            EventType.TASK_CREATED,
            summary,
            {"task": task, "mission": mission},
            mission_id=mission_id,
            agent_id=assigned_to,
        )
        return task

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | str | None = None,
        progress: float | None = None,
        assigned_to: str | None = None,
    ) -> Task:
        task = self.task(task_id)
        changes: dict[str, Any] = {}
        now = _now()
        if assigned_to is not None and assigned_to != task.assigned_to:
            self._check_agent(assigned_to, self.mission(task.mission_id))
            changes["assigned_to"] = assigned_to
        if progress is not None:
            changes["progress"] = progress
        if status is not None:
            status = TaskStatus(status).value
            changes["status"] = status
            if status not in _NOT_STARTED and task.started_at is None:
                changes["started_at"] = now
            if status in _TERMINAL:
                changes["completed_at"] = task.completed_at or now
                if status == TaskStatus.COMPLETED.value:
                    changes["progress"] = 1.0
            else:
                changes["completed_at"] = None
        task = self._replace(self._state.tasks, task, changes)
        await self._emit(
            EventType.TASK_UPDATED,
            self._task_summary(task, status_changed=status is not None),
            {"task": task},
            mission_id=task.mission_id,
            agent_id=task.assigned_to,
        )
        if status == TaskStatus.COMPLETED.value:
            await self._promote_ready(task.mission_id)
        return task

    async def _promote_ready(self, mission_id: str) -> None:
        for t in list(self.tasks_for(mission_id)):
            if t.status == TaskStatus.PENDING.value and t.depends_on and self._deps_met(t.depends_on):
                await self.update_task(t.id, status=TaskStatus.READY)

    # -- agents --------------------------------------------------------------

    async def set_agent_state(
        self,
        agent_id: str,
        status: AgentStatus | str,
        *,
        mission_id: str | None = None,
        activity: str | None = None,
        task_id: str | None = None,
        collaborating_with: list[str] | None = None,
    ) -> AgentState:
        self.agent_state(agent_id)  # validates the agent id
        with_ = list(collaborating_with or [])
        if task_id is not None:
            mission_id = mission_id or self.task(task_id).mission_id
        if mission_id is not None:
            mission = self.mission(mission_id)
            for a in (agent_id, *with_):
                self._check_agent(a, mission)
        else:
            for a in with_:
                self.registry.get(a)
        status = AgentStatus(status)
        state = AgentState(
            agent_id=agent_id,
            status=status,
            current_task_id=task_id,
            activity=activity,
            collaborating_with=with_,
        )
        _upsert(self._state.agent_states, state, "agent_id")
        name = self._name(agent_id)
        if with_:
            summary = f"{name} collaborating with {', '.join(self._name(a) for a in with_)}"
        else:
            summary = f"{name} · {status.value}"
        if activity:
            summary += f" · {activity}"
        await self._emit(
            EventType.AGENT_STATE_CHANGED,
            summary,
            {"state": state},
            mission_id=mission_id,
            agent_id=agent_id,
        )
        return state

    async def reset_agent(self, agent_id: str, mission_id: str | None = None) -> AgentState:
        return await self.set_agent_state(agent_id, self.default_status(agent_id), mission_id=mission_id)

    # -- communication -------------------------------------------------------

    async def send_message(
        self,
        mission_id: str,
        from_agent: str,
        to_agent: str,
        msg_type: MessageType | str,
        subject: str,
        body: str,
        *,
        task_id: str | None = None,
        confidence: Confidence | str | None = None,
        requires_response: bool = False,
        in_reply_to: str | None = None,
    ) -> AgentMessage:
        mission = self.mission(mission_id)
        self._check_agent(from_agent, mission)
        self._check_agent(to_agent, mission)
        if task_id is not None:
            self.task(task_id)
        message = AgentMessage(
            mission_id=mission_id,
            task_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            type=msg_type,
            subject=subject,
            body=body,
            confidence=confidence,
            requires_response=requires_response,
            in_reply_to=in_reply_to,
        )
        self._state.messages.append(message)
        await self._emit(
            EventType.MESSAGE_SENT,
            f"{self._name(from_agent)} → {self._name(to_agent)} · {message.type} · {subject}",
            {"message": message},
            mission_id=mission_id,
            agent_id=from_agent,
        )
        return message

    # -- reports -------------------------------------------------------------

    async def submit_report(
        self,
        mission_id: str,
        task_id: str,
        agent_id: str,
        asked_to: str,
        *,
        actions_taken: list[str] | None = None,
        inputs_used: list[str] | None = None,
        findings: list[Claim] | None = None,
        unresolved: list[str] | None = None,
        needs_agents: list[str] | None = None,
        confidence: Confidence | str = Confidence.MEDIUM,
        limitations: list[str] | None = None,
    ) -> AgentReport:
        mission = self.mission(mission_id)
        self._check_agent(agent_id, mission)
        task = self.task(task_id)
        report = AgentReport(
            mission_id=mission_id,
            task_id=task_id,
            agent_id=agent_id,
            asked_to=asked_to,
            actions_taken=actions_taken or [],
            inputs_used=inputs_used or [],
            findings=findings or [],
            unresolved=unresolved or [],
            needs_agents=needs_agents or [],
            confidence=confidence,
            limitations=limitations or [],
        )
        self._state.agent_reports.append(report)
        task = self._replace(self._state.tasks, task, {"result_report_id": report.id})
        await self._emit(
            EventType.REPORT_SUBMITTED,
            f"{self._name(agent_id)} submitted report for '{task.title}' ({report.confidence} confidence)",
            {"report": report, "task": task},
            mission_id=mission_id,
            agent_id=agent_id,
        )
        return report

    async def submit_mission_report(self, report: MissionReport) -> MissionReport:
        mission = self.mission(report.mission_id)
        _upsert(self._state.mission_reports, report)
        mission = self._replace(self._state.missions, mission, {"final_report_id": report.id})
        await self._emit(
            EventType.MISSION_REPORT_READY,
            f"{self._name(self.registry.orchestrator.id)} consolidated the mission report · "
            f"{report.objective_status}",
            {"report": report, "mission": mission},
            mission_id=mission.id,
            agent_id=self.registry.orchestrator.id,
        )
        return report

    # -- human in the loop ---------------------------------------------------

    async def request_approval(
        self,
        mission_id: str,
        requested_by: str,
        reason: ApprovalReason | str,
        title: str,
        detail: str,
        *,
        proposed_action: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalRequest:
        mission = self._open_mission(mission_id)
        self._check_agent(requested_by, mission)
        if task_id is not None:
            self.task(task_id)
        approval = ApprovalRequest(
            mission_id=mission_id,
            task_id=task_id,
            requested_by=requested_by,
            reason=reason,
            title=title,
            detail=detail,
            proposed_action=proposed_action,
        )
        self._state.approvals.append(approval)
        self._decisions[approval.id] = asyncio.Event()
        await self._emit(
            EventType.APPROVAL_REQUESTED,
            f"{self._name(requested_by)} requests approval · {title}",
            {"approval": approval},
            mission_id=mission_id,
            agent_id=requested_by,
        )
        return approval

    async def decide_approval(
        self, approval_id: str, decision: ApprovalState | str, note: str | None = None
    ) -> ApprovalRequest:
        approval = self.approval(approval_id)
        decision = ApprovalState(decision)
        if decision not in (ApprovalState.APPROVED, ApprovalState.REJECTED):
            raise StoreError("decision must be APPROVED or REJECTED")
        if approval.state != ApprovalState.PENDING.value:
            raise ConflictError(f"approval '{approval_id}' is already {approval.state}")
        approval = self._replace(
            self._state.approvals,
            approval,
            {"state": decision.value, "decision_note": note, "decided_at": _now()},
        )
        verb = "approved" if decision == ApprovalState.APPROVED else "rejected"
        await self._emit(
            EventType.APPROVAL_DECIDED,
            f"Human {verb} '{approval.title}'" + (f" · {note}" if note else ""),
            {"approval": approval},
            mission_id=approval.mission_id,
            agent_id=approval.requested_by,
        )
        if ev := self._decisions.get(approval_id):
            ev.set()
        return approval

    async def wait_for_decision(self, approval_id: str) -> ApprovalRequest:
        approval = self.approval(approval_id)
        if approval.state == ApprovalState.PENDING.value:
            ev = self._decisions.setdefault(approval_id, asyncio.Event())
            await ev.wait()
        self._decisions.pop(approval_id, None)
        return self.approval(approval_id)

    # -- misc ----------------------------------------------------------------

    async def log(
        self, text: str, *, mission_id: str | None = None, agent_id: str | None = None
    ) -> AtlasEvent:
        if agent_id is not None:
            self.registry.get(agent_id)
        return await self._emit(EventType.LOG, text, {}, mission_id=mission_id, agent_id=agent_id)

    async def reset(self) -> None:
        """Clear every mission (dev only). Emits a `log` event with `payload.reset`."""
        _clear(self._state)
        self._state.agent_states = self._initial_agent_states()
        for ev in self._decisions.values():
            ev.set()
        self._decisions.clear()
        await self._emit(
            EventType.LOG,
            "World reset · all missions cleared",
            {"reset": True, "agent_states": list(self._state.agent_states)},
            agent_id=self.registry.orchestrator.id,
        )

    # -- internals -----------------------------------------------------------

    def _initial_agent_states(self) -> list[AgentState]:
        return [AgentState(agent_id=a.id, status=self.default_status(a.id)) for a in self.registry.all()]

    async def _emit(
        self,
        etype: EventType,
        summary: str,
        payload: dict[str, Any],
        *,
        mission_id: str | None = None,
        agent_id: str | None = None,
    ) -> AtlasEvent:
        def enc(v: Any) -> Any:
            if isinstance(v, BaseModel):
                return _dump(v)
            if isinstance(v, list):
                return [enc(x) for x in v]
            return v

        event = AtlasEvent(
            type=etype,
            summary=summary,
            mission_id=mission_id,
            agent_id=agent_id,
            payload={k: enc(v) for k, v in payload.items()},
        )
        return await self.bus.publish(event)

    @staticmethod
    def _find(items: list[Any], ident: str, what: str) -> Any:
        for x in items:
            if x.id == ident:
                return x
        raise NotFoundError(f"unknown {what} '{ident}'")

    @staticmethod
    def _replace(items: list[Any], obj: Any, changes: dict[str, Any]) -> Any:
        data = obj.model_dump()
        data.update(changes)
        new = type(obj).model_validate(data)
        _upsert(items, new)
        return new

    def _open_mission(self, mission_id: str) -> Mission:
        mission = self.mission(mission_id)
        if mission.phase == MissionPhase.CLOSED.value:
            raise StoreError(f"mission '{mission_id}' is closed")
        return mission

    def _check_node(self, node: str) -> None:
        enabled = {n.id for n in self.registry.nodes()}
        if node not in enabled:
            try:
                self.registry.agents_for_node(node)
            except RegistryError as exc:
                raise NotFoundError(f"unknown node '{node}'") from exc
            raise StoreError(f"node '{node}' is disabled")

    def _check_agent(self, agent_id: str, mission: Mission) -> None:
        try:
            ok = self.registry.can_work_in(agent_id, mission.node)
        except RegistryError as exc:
            raise NotFoundError(str(exc)) from exc
        if not ok:
            raise IsolationError(
                f"agent '{agent_id}' may not work on mission '{mission.id}' in node '{mission.node}'"
            )

    def _deps_met(self, deps: list[str]) -> bool:
        return all(self.task(d).status == TaskStatus.COMPLETED.value for d in deps)

    def _name(self, agent_id: str | None) -> str:
        if not agent_id:
            return "—"
        try:
            return self.registry.get(agent_id).name
        except RegistryError:
            return agent_id

    def _node_name(self, node: str) -> str:
        return next((n.name for n in self.registry.nodes() if n.id == node), node)

    def _task_summary(self, task: Task, *, status_changed: bool) -> str:
        who = self._name(task.assigned_to)
        t = f"'{task.title}'"
        pct = f" ({round(task.progress * 100)}%)"
        if not status_changed:
            return f"{t} at {round(task.progress * 100)}%"
        return {
            TaskStatus.PENDING.value: f"{t} is pending",
            TaskStatus.READY.value: f"{t} is ready",
            TaskStatus.IN_PROGRESS.value: f"{who} started {t}" + (pct if task.progress else ""),
            TaskStatus.AWAITING_APPROVAL.value: f"{t} is awaiting human approval",
            TaskStatus.BLOCKED.value: f"{t} is blocked",
            TaskStatus.IN_REVIEW.value: f"{t} is in review",
            TaskStatus.COMPLETED.value: f"{who} completed {t}",
            TaskStatus.FAILED.value: f"{who} failed {t}",
            TaskStatus.CANCELLED.value: f"{t} was cancelled",
        }[task.status]


__all__ = [
    "ConflictError",
    "IsolationError",
    "NotFoundError",
    "StoreError",
    "WorldStore",
    "apply_event",
    "fold",
]
