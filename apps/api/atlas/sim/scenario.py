"""Scenario schema for simulated missions (see docs/SCENARIOS.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..core.models import (
    AgentStatus,
    ApprovalReason,
    Claim,
    Confidence,
    MessageType,
    MissionPhase,
    Priority,
    TaskStatus,
)
from ..core.registry import AgentRegistry

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    wait: float = Field(default=0.5, ge=0)


class PhaseStep(_Step):
    do: Literal["phase"]
    phase: MissionPhase


class TaskStep(_Step):
    do: Literal["task"]
    ref: str
    title: str
    description: str
    assigned_to: str
    priority: Priority = Priority.MEDIUM
    depends_on: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    approval_reason: ApprovalReason | None = None


class TaskUpdateStep(_Step):
    do: Literal["task_update"]
    ref: str
    status: TaskStatus | None = None
    progress: float | None = Field(default=None, ge=0, le=1)


class AgentStep(_Step):
    do: Literal["agent"]
    agent: str
    status: AgentStatus
    activity: str | None = None
    task: str | None = None
    with_: list[str] = Field(default_factory=list, alias="with")


class MessageStep(_Step):
    do: Literal["message"]
    from_: str = Field(alias="from")
    to: str
    type: MessageType
    subject: str
    body: str
    task: str | None = None
    confidence: Confidence | None = None
    requires_response: bool = False


class ReportStep(_Step):
    do: Literal["report"]
    agent: str
    task: str
    asked_to: str
    actions_taken: list[str] = Field(default_factory=list)
    inputs_used: list[str] = Field(default_factory=list)
    findings: list[Claim] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    needs_agents: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    limitations: list[str] = Field(default_factory=list)


class ApprovalStep(_Step):
    do: Literal["approval"]
    ref: str
    requested_by: str
    reason: ApprovalReason
    title: str
    detail: str
    proposed_action: str | None = None
    task: str | None = None
    on_reject: list[Step] = Field(default_factory=list)


class MissionReportStep(_Step):
    do: Literal["mission_report"]
    executive_summary: str
    objective_status: Literal["ACHIEVED", "PARTIAL", "NOT_ACHIEVED"]
    key_findings: list[Claim] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    needs_human_attention: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class LogStep(_Step):
    do: Literal["log"]
    agent: str | None = None
    text: str


Step = Annotated[
    PhaseStep | TaskStep | TaskUpdateStep | AgentStep | MessageStep | ReportStep | ApprovalStep | MissionReportStep | LogStep,
    Field(discriminator="do"),
]
ApprovalStep.model_rebuild()


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    node: str
    title: str
    objective: str
    steps: list[Step]


class ScenarioError(ValueError):
    pass


def validate_scenario(sc: Scenario, registry: AgentRegistry) -> None:
    """Semantic checks beyond the schema (see 'Rules' in docs/SCENARIOS.md)."""
    task_refs: set[str] = set()

    def agent_ok(agent_id: str, where: str) -> None:
        try:
            registry.get(agent_id)
        except ValueError as exc:
            raise ScenarioError(f"{sc.id}: {where}: unknown agent '{agent_id}'") from exc
        if not registry.can_work_in(agent_id, sc.node):
            raise ScenarioError(f"{sc.id}: {where}: agent '{agent_id}' may not work in node '{sc.node}'")

    def ref_ok(ref: str | None, where: str) -> None:
        if ref is not None and ref not in task_refs:
            raise ScenarioError(f"{sc.id}: {where}: task ref '{ref}' used before it was created")

    def walk(steps: list, prefix: str) -> None:
        for i, st in enumerate(steps):
            where = f"{prefix}step {i} ({st.do})"
            if isinstance(st, TaskStep):
                agent_ok(st.assigned_to, where)
                for dep in st.depends_on:
                    ref_ok(dep, where)
                task_refs.add(st.ref)
            elif isinstance(st, TaskUpdateStep):
                ref_ok(st.ref, where)
            elif isinstance(st, AgentStep):
                agent_ok(st.agent, where)
                for other in st.with_:
                    agent_ok(other, where)
                ref_ok(st.task, where)
            elif isinstance(st, MessageStep):
                agent_ok(st.from_, where)
                agent_ok(st.to, where)
                ref_ok(st.task, where)
            elif isinstance(st, ReportStep):
                agent_ok(st.agent, where)
                ref_ok(st.task, where)
            elif isinstance(st, ApprovalStep):
                agent_ok(st.requested_by, where)
                ref_ok(st.task, where)
                walk(st.on_reject, f"{where} on_reject ")
            elif isinstance(st, LogStep) and st.agent:
                agent_ok(st.agent, where)

    if sc.node not in {n.id for n in registry.nodes()} and sc.node not in {"personal"}:
        raise ScenarioError(f"{sc.id}: unknown node '{sc.node}'")
    walk(sc.steps, "")
    last = sc.steps[-1] if sc.steps else None
    if not (isinstance(last, PhaseStep) and last.phase == MissionPhase.CLOSED):
        raise ScenarioError(f"{sc.id}: must end with phase: CLOSED")


def load_scenarios(registry: AgentRegistry, directory: Path = SCENARIOS_DIR) -> dict[str, Scenario]:
    scenarios: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.y*ml")):
        sc = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        validate_scenario(sc, registry)
        if sc.id in scenarios:
            raise ScenarioError(f"duplicate scenario id '{sc.id}'")
        scenarios[sc.id] = sc
    return scenarios
