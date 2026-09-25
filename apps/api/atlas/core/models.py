"""ATLAS core contracts.

This module is the single source of truth for every data shape that flows
between the orchestrator, the agents, the API and the Command Center UI.
The TypeScript types in `contracts/atlas.ts` are generated from these models
(`python scripts/export_schema.py && npm run gen:types`), so change them here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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


class BrowserConfig(AtlasModel):
    """Opt-in browser for one agent: its own dedicated, persistent browser profile (never the user's Chrome).

    The human logs into the site once in that profile (`atlas-browser login <agent>`); the agent then opens,
    reads, clicks, fills filters, extracts tables and downloads files, only on `allowed_domains`.
    Strings may use ${ENV_VAR} so personal sites stay out of the public repo."""

    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", description="profile folder in <ATLAS_LOCAL_DIR>/browser/")
    start_url: str = ""
    allowed_domains: list[str] = Field(
        default_factory=list, description="hosts the agent may open (subdomains included); comma lists allowed"
    )
    max_turns: int = Field(default=40, ge=4, le=200, description="turn budget of tasks that use the browser")


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
    plannable: bool = Field(default=True, description="false = never assigned tasks by the planner (e.g. AUDITOR)")
    browser: BrowserConfig | None = Field(default=None, description="opt-in dedicated browser (docs/BROWSER.md)")


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


HUMAN = "human"  # pseudo agent id for messages from the user (mission thread)


class Attachment(AtlasModel):
    id: str = Field(default_factory=lambda: _id("att"))
    name: str
    kind: Literal["text", "markdown", "json", "file", "url"] = "text"
    uri: str | None = None
    content: str | None = None
    size_bytes: int | None = None
    download_url: str | None = Field(default=None, description="API path to download a file ATLAS stores")


class Evidence(AtlasModel):
    """A record of something an agent actually did, written by the system (never by the agent).

    Reports and the activity feed are built from these, so claimed actions are provable."""

    id: str = Field(default_factory=lambda: _id("evd"))
    mission_id: str
    task_id: str | None = None
    agent_id: str
    kind: Literal[
        "file_listed", "file_read", "file_written", "web_search", "web_fetch", "consult", "approval",
        "email_read", "draft_created", "external_call", "browser_visit", "browser_action", "browser_download",
    ]
    ref: str = Field(description="path, URL, agent id or approval id")
    detail: str = ""
    ok: bool = True
    at: datetime = Field(default_factory=_now)


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
    round: int = Field(default=1, description="1 = initial plan; 2+ = follow-up rounds from the mission thread")
    retries: int = Field(default=0, description="automatic re-runs after a transient failure")
    revision_of: str | None = Field(default=None, description="task id whose report AUDITOR sent back for revision")
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
    usage_by_agent: dict[str, Usage] = Field(default_factory=dict, description="agent id -> usage (same totals)")
    context: str | None = None
    phase: MissionPhase = MissionPhase.OBJECTIVE
    priority: Priority = Priority.MEDIUM
    task_ids: list[str] = Field(default_factory=list)
    final_report_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list, description="files the user attached")
    round: int = 1
    interrupted: bool = Field(default=False, description="was running when the server stopped")
    publish: bool = Field(default=True, description="SCRIBE formats the report as institutional documents")
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
    evidence: list[Evidence] = Field(default_factory=list, description="system-recorded actions of this task")
    deliverables: list[Attachment] = Field(default_factory=list, description="files this task wrote")
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
    version: int = Field(default=1, description="bumps with each follow-up round")
    deliverables: list[Attachment] = Field(default_factory=list)
    documents: list[Attachment] = Field(
        default_factory=list, description="institutional documents SCRIBE rendered from this report (PDF, deck)")
    audit_summary: str = Field(default="", description="what AUDITOR checked and what it found (system-written)")
    untraced: list[str] = Field(
        default_factory=list,
        description="figures in this report that appear in no agent report or evidence (system check)")
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Audit (AUDITOR, docs/AUDITOR.md)
# ---------------------------------------------------------------------------


class AuditVerdict(str, Enum):
    PASS = "PASS"  # the report holds up against its evidence
    ISSUES = "ISSUES"  # usable, with the listed caveats
    FAIL = "FAIL"  # key findings are unsupported or wrong: sent back for revision


class AuditIssue(AtlasModel):
    finding: str = Field(description="the finding or passage questioned, quoted")
    problem: str
    kind: Literal["unsupported", "mislabeled", "inconsistent", "calculation", "stale", "scope", "other"] = "other"
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


class Audit(AtlasModel):
    """AUDITOR's check of one agent report against the evidence the system recorded for its task."""

    id: str = Field(default_factory=lambda: _id("aud"))
    mission_id: str
    round: int = 1
    agent_report_id: str
    task_id: str
    agent_id: str
    verdict: AuditVerdict
    summary: str = ""
    issues: list[AuditIssue] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list, description="deterministic checks the system ran first")
    revision_task_id: str | None = Field(default=None, description="the revision task opened for a FAIL")
    final: bool = Field(default=False, description="audit of a revision: no further revision is opened")
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Inbox: follow-ups and email drafts (docs/INBOX.md)
# ---------------------------------------------------------------------------


class EmailRef(AtlasModel):
    """Minimal pointer to the source email. Full bodies are never stored."""

    message_id: str
    subject: str
    sender: str
    received_at: datetime | None = None
    excerpt: str = Field(default="", description="the few lines that support the follow-up (≤ 400 chars)")
    web_link: str | None = None


class FollowUp(AtlasModel):
    id: str = Field(default_factory=lambda: _id("fup"))
    node: str = "corporate"
    kind: Literal["MY_COMMITMENT", "THEIR_COMMITMENT", "AWAITING_REPLY", "REQUEST_TO_ME"]
    title: str
    detail: str = ""
    counterpart: str | None = Field(default=None, description="the other person (name <email>)")
    due: date | None = None
    status: Literal["OPEN", "WAITING", "DONE", "DISMISSED"] = "OPEN"
    priority: Priority = Priority.MEDIUM
    source: EmailRef | None = None
    draft_id: str | None = None
    mission_id: str | None = Field(default=None, description="the inbox scan that found it")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EmailDraft(AtlasModel):
    """A reply or follow-up ALFRED drafted. ATLAS never sends email."""

    id: str = Field(default_factory=lambda: _id("drf"))
    node: str = "corporate"
    followup_id: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    in_reply_to: str | None = Field(default=None, description="source message id")
    status: Literal["PROPOSED", "APPROVED", "DISCARDED", "EXPORTED"] = "PROPOSED"
    export: Literal["eml", "outlook_drafts"] | None = None
    download_url: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class DigestThread(AtlasModel):
    """One conversation where the user is only in CC, summarized."""

    conversation_id: str
    subject: str
    project: str | None = Field(default=None, description="from the user's local project keywords")
    participants: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list, description="3-5 bullets: what happened")
    decisions: list[str] = Field(default_factory=list)
    figures: list[str] = Field(default_factory=list, description="key numbers with their context")
    asks_me: str | None = Field(default=None, description="what someone asked the user, if anything")
    importance: Priority = Priority.MEDIUM
    messages: list[EmailRef] = Field(default_factory=list)
    followup_id: str | None = None


class Digest(AtlasModel):
    """A CC briefing produced by an inbox scan (docs/INBOX.md §4)."""

    id: str = Field(default_factory=lambda: _id("dig"))
    node: str = "corporate"
    mission_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    headline: list[str] = Field(default_factory=list, description="≤ 3 lines: what matters most")
    threads: list[DigestThread] = Field(default_factory=list)
    skipped: int = Field(default=0, description="CC emails excluded by rules or as automated")
    documents: list[Attachment] = Field(default_factory=list, description="SCRIBE's institutional PDF of the digest")
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Monitoring (ARGOS): alerts, Rocks, weekly brief (docs/ARGOS.md)
# ---------------------------------------------------------------------------


class AlertEvidence(AtlasModel):
    """A verbatim line from a source, so every alert can be checked."""

    source: str = Field(description="file name, API resource or email subject")
    version: Literal["previous", "current", "single"] = "single"
    quote: str = Field(description="verbatim text (≤ 300 chars)")


class Alert(AtlasModel):
    id: str = Field(default_factory=lambda: _id("alr"))
    node: str = "corporate"
    check: str = Field(description="which check raised it: dashboards | l10 | rocks | …")
    kind: Literal[
        "missing_report", "identical_report", "moved_date", "removed_row", "kpi_mismatch", "value_change",
        "overdue_todo", "unreported_todo", "stale_issue", "rock_failed", "rock_at_risk", "other",
    ]
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    project: str | None = None
    title: str
    detail: str = ""
    evidence: list[AlertEvidence] = Field(default_factory=list)
    fingerprint: str = Field(description="stable key so the same finding updates instead of duplicating")
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] = "OPEN"
    first_seen: datetime = Field(default_factory=_now)
    last_seen: datetime = Field(default_factory=_now)
    mission_id: str | None = None


class RockStatus(AtlasModel):
    """A Rock tracked by ARGOS from the user's private rocks file."""

    id: str
    title: str
    owner: str
    project: str | None = None
    quarter: str
    due: date
    metric: str | None = Field(default=None, description="e.g. 'unidades escrituradas'")
    target: float | None = None
    current: float | None = None
    start_value: float | None = None
    start_date: date | None = None
    status: Literal["ON_TRACK", "AT_RISK", "OFF_TRACK", "DONE", "FAILED", "UNKNOWN"] = "UNKNOWN"
    reason: str = ""
    required_pace: float | None = Field(default=None, description="units per week needed from today")
    observed_pace: float | None = Field(default=None, description="units per week so far")
    updated_at: datetime = Field(default_factory=_now)


class Brief(AtlasModel):
    """The weekly L10 brief ARGOS prepares (a deliverable plus a structured summary)."""

    id: str = Field(default_factory=lambda: _id("brf"))
    node: str = "corporate"
    week: str = Field(description="ISO week, e.g. 2026-W40")
    headline: list[str] = Field(default_factory=list)
    sections: dict[str, list[str]] = Field(default_factory=dict)
    deliverable: Attachment | None = None
    documents: list[Attachment] = Field(default_factory=list, description="SCRIBE's institutional PDF + deck")
    mission_id: str | None = None
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
    MISSION_UPDATED = "mission.updated"
    MISSION_CLOSED = "mission.closed"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    AGENT_STATE_CHANGED = "agent.state_changed"
    MESSAGE_SENT = "message.sent"
    REPORT_SUBMITTED = "report.submitted"
    MISSION_REPORT_READY = "mission.report_ready"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    EVIDENCE_RECORDED = "evidence.recorded"
    FOLLOWUP_UPSERTED = "followup.upserted"
    DRAFT_UPSERTED = "draft.upserted"
    DIGEST_READY = "digest.ready"
    ALERT_UPSERTED = "alert.upserted"
    ROCK_UPDATED = "rock.updated"
    BRIEF_READY = "brief.ready"
    AUDIT_RECORDED = "audit.recorded"
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
    evidence: list[Evidence] = Field(default_factory=list)
    followups: list[FollowUp] = Field(default_factory=list)
    drafts: list[EmailDraft] = Field(default_factory=list)
    digests: list[Digest] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    rocks: list[RockStatus] = Field(default_factory=list)
    briefs: list[Brief] = Field(default_factory=list)
    audits: list[Audit] = Field(default_factory=list)
    last_seq: int = 0
