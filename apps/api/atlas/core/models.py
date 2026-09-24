"""ATLAS core contracts.

This module is the single source of truth for every data shape that flows
between the orchestrator, the agents, the API and the Command Center UI.
The TypeScript types in `contracts/atlas.ts` are generated from these models
(`python scripts/export_schema.py && npm run gen:types`), so change them here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


class AtlasModel(BaseModel):
    model_config = ConfigDict(
        use_enum_values=True,
        populate_by_name=True,
        # fields with defaults are always present in API output -> required in the TS types
        json_schema_serialization_defaults_required=True,
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    """Operational status shown on the Agent Board."""

    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    COLLABORATING = "COLLABORATING"
    REVIEWING = "REVIEWING"
    MONITORING = "MONITORING"  # added: ARGOS' steady state (used in the spec's board example)
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class TaskStatus(str, Enum):
    PENDING = "PENDING"  # created, dependencies not met yet
    READY = "READY"  # dependencies met, not yet picked up
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    IN_REVIEW = "IN_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MissionPhase(str, Enum):
    """The nine-step lifecycle from the project spec."""

    OBJECTIVE = "OBJECTIVE"
    DECOMPOSITION = "DECOMPOSITION"
    DELEGATION = "DELEGATION"
    EXECUTION = "EXECUTION"
    COLLABORATION = "COLLABORATION"
    VALIDATION = "VALIDATION"
    CONSOLIDATION = "CONSOLIDATION"
    REPORTING = "REPORTING"
    FOLLOW_UP = "FOLLOW_UP"
    CLOSED = "CLOSED"


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MessageType(str, Enum):
    REQUEST = "REQUEST"
    RESULT = "RESULT"
    ALERT = "ALERT"
    QUESTION = "QUESTION"
    ANSWER = "ANSWER"
    REVIEW = "REVIEW"


class Confidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalReason(str, Enum):
    """Why a human must intervene (spec section 11)."""

    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    FINANCIAL_COMMITMENT = "FINANCIAL_COMMITMENT"
    CONSEQUENTIAL_DECISION = "CONSEQUENTIAL_DECISION"
    AMBIGUOUS_OR_CONFLICTING = "AMBIGUOUS_OR_CONFLICTING"
    IRREVERSIBLE_ACTION = "IRREVERSIBLE_ACTION"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"


class ApprovalState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ClaimKind(str, Enum):
    """ORACLE's rule: never present an assumption as a fact."""

    FACT = "FACT"
    ASSUMPTION = "ASSUMPTION"
    SCENARIO = "SCENARIO"
    RECOMMENDATION = "RECOMMENDATION"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentKind(str, Enum):
    NATIVE = "native"  # runs inside ATLAS on the Claude API
    EXTERNAL = "external"  # an existing agent plugged in through an adapter


class AdapterType(str, Enum):
    CLAUDE = "claude"  # native: Claude API with a role prompt + tools
    HTTP = "http"  # external: POST a Task, receive a Report (webhook, n8n, Make, custom API)
    MCP = "mcp"  # external: an MCP server whose tools the agent exposes
    CLI = "cli"  # external: a local script/command (stdin Task JSON -> stdout Report JSON)
    CLAUDE_MD = "claude_md"  # external: a Claude Code agent file (.md) run on the Claude API; prompt stays local
    MOCK = "mock"  # scripted agent for the simulated Command Center


class AgentPermissions(AtlasModel):
    can_delegate: bool = False
    can_message: list[str] = Field(default_factory=lambda: ["*"], description="agent ids, '*' = all")
    requires_approval_for: list[ApprovalReason] = Field(default_factory=list)
    max_parallel_tasks: int = 1


class NodeDefinition(AtlasModel):
    """An isolated operating context (e.g. corporate, personal).

    Isolation rule: a mission lives in exactly one node. Node-bound agents only work on missions of
    their node; shared agents (nodes: ['*']) serve every node but never carry context between them.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    description: str = ""
    color: str = Field(default="#7dd3fc", pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True


class DivisionDefinition(AtlasModel):
    """A team of specialist agents shown as one expandable unit (e.g. the EOS division)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    node: str
    description: str = ""
    color: str = Field(default="#7dd3fc", pattern=r"^#[0-9a-fA-F]{6}$")
    lead: str | None = Field(default=None, description="agent id that triages work for the division")


class AgentDefinition(AtlasModel):
    """One file in /agents. Adding an agent = adding a YAML file."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    title: str
    description: str
    color: str = Field(default="#7dd3fc", pattern=r"^#[0-9a-fA-F]{6}$")
    kind: AgentKind = AgentKind.NATIVE
    adapter: AdapterType = AdapterType.CLAUDE
    adapter_config: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    system_prompt: str | None = None
    enabled: bool = True
    is_orchestrator: bool = False
    nodes: list[str] = Field(default_factory=lambda: ["*"], description="node ids, '*' = shared core agent")
    division: str | None = None


class AgentState(AtlasModel):
    """Live runtime state of one agent (what the Agent Board renders)."""

    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: str | None = None
    activity: str | None = Field(default=None, description="one-line 'what I'm doing now'")
    collaborating_with: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Missions and tasks
# ---------------------------------------------------------------------------


class Attachment(AtlasModel):
    id: str = Field(default_factory=lambda: _id("att"))
    name: str
    kind: Literal["text", "markdown", "json", "file", "url"] = "text"
    uri: str | None = None
    content: str | None = None


class Task(AtlasModel):
    id: str = Field(default_factory=lambda: _id("tsk"))
    mission_id: str
    title: str
    description: str
    assigned_to: str | None = Field(default=None, description="agent id")
    created_by: str = "atlas"
    status: TaskStatus = TaskStatus.PENDING
    priority: Priority = Priority.MEDIUM
    depends_on: list[str] = Field(default_factory=list, description="task ids")
    requires_approval: bool = False
    approval_reason: ApprovalReason | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    parent_task_id: str | None = None
    result_report_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Usage(AtlasModel):
    """Token usage and estimated cost (USD) for a mission or agent."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0
    est_cost_usd: float = 0.0


class Mission(AtlasModel):
    id: str = Field(default_factory=lambda: _id("msn"))
    objective: str
    node: str = "corporate"
    mode: Literal["simulated", "live"] = "simulated"
    usage: Usage = Field(default_factory=Usage)
    context: str | None = None
    phase: MissionPhase = MissionPhase.OBJECTIVE
    priority: Priority = Priority.MEDIUM
    task_ids: list[str] = Field(default_factory=list)
    final_report_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    closed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Communication
# ---------------------------------------------------------------------------


class AgentMessage(AtlasModel):
    """Structured agent-to-agent message (spec section 9)."""

    id: str = Field(default_factory=lambda: _id("msg"))
    mission_id: str
    task_id: str | None = None
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    type: MessageType
    subject: str
    body: str
    attachments: list[Attachment] = Field(default_factory=list)
    confidence: Confidence | None = None
    requires_response: bool = False
    in_reply_to: str | None = None
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class Claim(AtlasModel):
    kind: ClaimKind
    statement: str
    sources: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM


class AgentReport(AtlasModel):
    """Agent-level report (spec section 10)."""

    id: str = Field(default_factory=lambda: _id("rpt"))
    mission_id: str
    task_id: str
    agent_id: str
    asked_to: str
    actions_taken: list[str] = Field(default_factory=list)
    inputs_used: list[str] = Field(default_factory=list)
    findings: list[Claim] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    needs_agents: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    limitations: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class MissionReport(AtlasModel):
    """ATLAS-level executive consolidation (spec section 10)."""

    id: str = Field(default_factory=lambda: _id("rpt"))
    mission_id: str
    executive_summary: str
    objective_status: Literal["ACHIEVED", "PARTIAL", "NOT_ACHIEVED"]
    tasks_completed: list[str] = Field(default_factory=list)
    tasks_pending: list[str] = Field(default_factory=list)
    key_findings: list[Claim] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    needs_human_attention: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    agent_report_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Human in the loop
# ---------------------------------------------------------------------------


class ApprovalRequest(AtlasModel):
    id: str = Field(default_factory=lambda: _id("apr"))
    mission_id: str
    task_id: str | None = None
    requested_by: str
    reason: ApprovalReason
    title: str
    detail: str
    proposed_action: str | None = None
    options: list[str] = Field(default_factory=lambda: ["APPROVE", "REJECT"])
    state: ApprovalState = ApprovalState.PENDING
    decision_note: str | None = None
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


# ---------------------------------------------------------------------------
# Event stream (backend -> Command Center, over WebSocket)
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    MISSION_CREATED = "mission.created"
    MISSION_PHASE_CHANGED = "mission.phase_changed"
    MISSION_CLOSED = "mission.closed"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    AGENT_STATE_CHANGED = "agent.state_changed"
    MESSAGE_SENT = "message.sent"
    REPORT_SUBMITTED = "report.submitted"
    MISSION_REPORT_READY = "mission.report_ready"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    LOG = "log"


class AtlasEvent(AtlasModel):
    """Every event carries a human-readable `summary` for the Activity Feed
    and a typed `payload` for the widgets that react to it."""

    id: str = Field(default_factory=lambda: _id("evt"))
    seq: int = 0
    type: EventType
    mission_id: str | None = None
    agent_id: str | None = None
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Snapshot (GET /state) — everything the Command Center needs on load
# ---------------------------------------------------------------------------


class WorldState(AtlasModel):
    nodes: list[NodeDefinition] = Field(default_factory=list)
    divisions: list[DivisionDefinition] = Field(default_factory=list)
    agents: list[AgentDefinition] = Field(default_factory=list)
    agent_states: list[AgentState] = Field(default_factory=list)
    missions: list[Mission] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    agent_reports: list[AgentReport] = Field(default_factory=list)
    mission_reports: list[MissionReport] = Field(default_factory=list)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    last_seq: int = 0
