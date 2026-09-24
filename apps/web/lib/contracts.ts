/* Generated from contracts/atlas.schema.json — do not edit. Run: npm run gen:types */

export type AgentKind = "native" | "external";
export type AdapterType = "claude" | "http" | "mcp" | "cli" | "claude_md" | "mock";
/**
 * Why a human must intervene (spec section 11).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "ApprovalReason".
 */
export type ApprovalReason =
  | "EXTERNAL_COMMUNICATION"
  | "FINANCIAL_COMMITMENT"
  | "CONSEQUENTIAL_DECISION"
  | "AMBIGUOUS_OR_CONFLICTING"
  | "IRREVERSIBLE_ACTION"
  | "INSUFFICIENT_INFORMATION";
/**
 * Operational status shown on the Agent Board.
 */
export type AgentStatus =
  "IDLE" | "WORKING" | "WAITING" | "COLLABORATING" | "REVIEWING" | "MONITORING" | "BLOCKED" | "COMPLETED" | "ERROR";
/**
 * The nine-step lifecycle from the project spec.
 */
export type MissionPhase =
  | "OBJECTIVE"
  | "DECOMPOSITION"
  | "DELEGATION"
  | "EXECUTION"
  | "COLLABORATION"
  | "VALIDATION"
  | "CONSOLIDATION"
  | "REPORTING"
  | "FOLLOW_UP"
  | "CLOSED";
export type Priority = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type TaskStatus =
  | "PENDING"
  | "READY"
  | "IN_PROGRESS"
  | "AWAITING_APPROVAL"
  | "BLOCKED"
  | "IN_REVIEW"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
export type Priority1 = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "MessageType".
 */
export type MessageType = "REQUEST" | "RESULT" | "ALERT" | "QUESTION" | "ANSWER" | "REVIEW";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Confidence".
 */
export type Confidence = "LOW" | "MEDIUM" | "HIGH";
/**
 * ORACLE's rule: never present an assumption as a fact.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "ClaimKind".
 */
export type ClaimKind = "FACT" | "ASSUMPTION" | "SCENARIO" | "RECOMMENDATION";
export type Confidence1 = "LOW" | "MEDIUM" | "HIGH";
export type Confidence2 = "LOW" | "MEDIUM" | "HIGH";
export type ApprovalState = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "EventType".
 */
export type EventType =
  | "mission.created"
  | "mission.phase_changed"
  | "mission.updated"
  | "mission.closed"
  | "task.created"
  | "task.updated"
  | "agent.state_changed"
  | "message.sent"
  | "report.submitted"
  | "mission.report_ready"
  | "approval.requested"
  | "approval.decided"
  | "evidence.recorded"
  | "followup.upserted"
  | "draft.upserted"
  | "digest.ready"
  | "alert.upserted"
  | "rock.updated"
  | "brief.ready"
  | "log";
export type Priority2 = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type Priority3 = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AdapterType".
 */
export type AdapterType1 = "claude" | "http" | "mcp" | "cli" | "claude_md" | "mock";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentKind".
 */
export type AgentKind1 = "native" | "external";
/**
 * Operational status shown on the Agent Board.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentStatus".
 */
export type AgentStatus1 =
  "IDLE" | "WORKING" | "WAITING" | "COLLABORATING" | "REVIEWING" | "MONITORING" | "BLOCKED" | "COMPLETED" | "ERROR";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "ApprovalState".
 */
export type ApprovalState1 = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED";
/**
 * The nine-step lifecycle from the project spec.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "MissionPhase".
 */
export type MissionPhase1 =
  | "OBJECTIVE"
  | "DECOMPOSITION"
  | "DELEGATION"
  | "EXECUTION"
  | "COLLABORATION"
  | "VALIDATION"
  | "CONSOLIDATION"
  | "REPORTING"
  | "FOLLOW_UP"
  | "CLOSED";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Priority".
 */
export type Priority4 = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "TaskStatus".
 */
export type TaskStatus1 =
  | "PENDING"
  | "READY"
  | "IN_PROGRESS"
  | "AWAITING_APPROVAL"
  | "BLOCKED"
  | "IN_REVIEW"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export interface AtlasContracts {
  NodeDefinition?: NodeDefinition;
  DivisionDefinition?: DivisionDefinition;
  AgentDefinition?: AgentDefinition;
  AgentState?: AgentState;
  Mission?: Mission;
  Task?: Task;
  AgentMessage?: AgentMessage;
  AgentReport?: AgentReport;
  MissionReport?: MissionReport;
  ApprovalRequest?: ApprovalRequest;
  AtlasEvent?: AtlasEvent;
  Evidence?: Evidence;
  FollowUp?: FollowUp;
  EmailDraft?: EmailDraft;
  Digest?: Digest;
  Alert?: Alert;
  RockStatus?: RockStatus;
  Brief?: Brief;
  WorldState?: WorldState;
}
/**
 * An isolated operating context (e.g. corporate, personal).
 *
 * Isolation rule: a mission lives in exactly one node. Node-bound agents only work on missions of
 * their node; shared agents (nodes: ['*']) serve every node but never carry context between them.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "NodeDefinition".
 */
export interface NodeDefinition {
  id: string;
  name: string;
  description: string;
  color: string;
  enabled: boolean;
}
/**
 * A team of specialist agents shown as one expandable unit (e.g. the EOS division).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "DivisionDefinition".
 */
export interface DivisionDefinition {
  id: string;
  name: string;
  node: string;
  description: string;
  color: string;
  /**
   * agent id that triages work for the division
   */
  lead: string | null;
}
/**
 * One file in /agents. Adding an agent = adding a YAML file.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentDefinition".
 */
export interface AgentDefinition {
  id: string;
  name: string;
  title: string;
  description: string;
  color: string;
  kind: AgentKind;
  adapter: AdapterType;
  adapter_config: {
    [k: string]: unknown;
  };
  model: string | null;
  capabilities: string[];
  tools: string[];
  permissions: AgentPermissions;
  system_prompt: string | null;
  enabled: boolean;
  is_orchestrator: boolean;
  /**
   * node ids, '*' = shared core agent
   */
  nodes: string[];
  division: string | null;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentPermissions".
 */
export interface AgentPermissions {
  can_delegate: boolean;
  /**
   * agent ids, '*' = all
   */
  can_message: string[];
  requires_approval_for: ApprovalReason[];
  max_parallel_tasks: number;
}
/**
 * Live runtime state of one agent (what the Agent Board renders).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentState".
 */
export interface AgentState {
  agent_id: string;
  status: AgentStatus;
  current_task_id: string | null;
  /**
   * one-line 'what I'm doing now'
   */
  activity: string | null;
  collaborating_with: string[];
  updated_at: string;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Mission".
 */
export interface Mission {
  id: string;
  objective: string;
  node: string;
  mode: "simulated" | "live";
  usage: Usage;
  context: string | null;
  phase: MissionPhase;
  priority: Priority;
  task_ids: string[];
  final_report_id: string | null;
  /**
   * files the user attached
   */
  attachments: Attachment[];
  round: number;
  /**
   * was running when the server stopped
   */
  interrupted: boolean;
  created_at: string;
  closed_at: string | null;
}
/**
 * Token usage and estimated cost (USD) for a mission or agent.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Usage".
 */
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  llm_calls: number;
  est_cost_usd: number;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Attachment".
 */
export interface Attachment {
  id: string;
  name: string;
  kind: "text" | "markdown" | "json" | "file" | "url";
  uri: string | null;
  content: string | null;
  size_bytes: number | null;
  /**
   * API path to download a file ATLAS stores
   */
  download_url: string | null;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Task".
 */
export interface Task {
  id: string;
  mission_id: string;
  title: string;
  description: string;
  /**
   * agent id
   */
  assigned_to: string | null;
  created_by: string;
  status: TaskStatus;
  priority: Priority1;
  /**
   * task ids
   */
  depends_on: string[];
  requires_approval: boolean;
  approval_reason: ApprovalReason | null;
  progress: number;
  parent_task_id: string | null;
  result_report_id: string | null;
  /**
   * 1 = initial plan; 2+ = follow-up rounds from the mission thread
   */
  round: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}
/**
 * Structured agent-to-agent message (spec section 9).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentMessage".
 */
export interface AgentMessage {
  id: string;
  mission_id: string;
  task_id: string | null;
  from: string;
  to: string;
  type: MessageType;
  subject: string;
  body: string;
  attachments: Attachment[];
  confidence: Confidence | null;
  requires_response: boolean;
  in_reply_to: string | null;
  created_at: string;
}
/**
 * Agent-level report (spec section 10).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AgentReport".
 */
export interface AgentReport {
  id: string;
  mission_id: string;
  task_id: string;
  agent_id: string;
  asked_to: string;
  actions_taken: string[];
  inputs_used: string[];
  findings: Claim[];
  unresolved: string[];
  needs_agents: string[];
  confidence: Confidence2;
  limitations: string[];
  attachments: Attachment[];
  /**
   * system-recorded actions of this task
   */
  evidence: Evidence[];
  /**
   * files this task wrote
   */
  deliverables: Attachment[];
  created_at: string;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Claim".
 */
export interface Claim {
  kind: ClaimKind;
  statement: string;
  sources: string[];
  confidence: Confidence1;
}
/**
 * A record of something an agent actually did, written by the system (never by the agent).
 *
 * Reports and the activity feed are built from these, so claimed actions are provable.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Evidence".
 */
export interface Evidence {
  id: string;
  mission_id: string;
  task_id: string | null;
  agent_id: string;
  kind:
    | "file_listed"
    | "file_read"
    | "file_written"
    | "web_search"
    | "web_fetch"
    | "consult"
    | "approval"
    | "email_read"
    | "draft_created";
  /**
   * path, URL, agent id or approval id
   */
  ref: string;
  detail: string;
  ok: boolean;
  at: string;
}
/**
 * ATLAS-level executive consolidation (spec section 10).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "MissionReport".
 */
export interface MissionReport {
  id: string;
  mission_id: string;
  executive_summary: string;
  objective_status: "ACHIEVED" | "PARTIAL" | "NOT_ACHIEVED";
  tasks_completed: string[];
  tasks_pending: string[];
  key_findings: Claim[];
  conflicts: string[];
  assumptions: string[];
  needs_human_attention: string[];
  next_actions: string[];
  references: string[];
  agent_report_ids: string[];
  /**
   * bumps with each follow-up round
   */
  version: number;
  deliverables: Attachment[];
  created_at: string;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "ApprovalRequest".
 */
export interface ApprovalRequest {
  id: string;
  mission_id: string;
  task_id: string | null;
  requested_by: string;
  reason: ApprovalReason;
  title: string;
  detail: string;
  proposed_action: string | null;
  options: string[];
  state: ApprovalState;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
}
/**
 * Every event carries a human-readable `summary` for the Activity Feed
 * and a typed `payload` for the widgets that react to it.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AtlasEvent".
 */
export interface AtlasEvent {
  id: string;
  seq: number;
  type: EventType;
  mission_id: string | null;
  agent_id: string | null;
  summary: string;
  payload: {
    [k: string]: unknown;
  };
  ts: string;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "FollowUp".
 */
export interface FollowUp {
  id: string;
  node: string;
  kind: "MY_COMMITMENT" | "THEIR_COMMITMENT" | "AWAITING_REPLY" | "REQUEST_TO_ME";
  title: string;
  detail: string;
  /**
   * the other person (name <email>)
   */
  counterpart: string | null;
  due: string | null;
  status: "OPEN" | "WAITING" | "DONE" | "DISMISSED";
  priority: Priority2;
  source: EmailRef | null;
  draft_id: string | null;
  /**
   * the inbox scan that found it
   */
  mission_id: string | null;
  created_at: string;
  updated_at: string;
}
/**
 * Minimal pointer to the source email. Full bodies are never stored.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "EmailRef".
 */
export interface EmailRef {
  message_id: string;
  subject: string;
  sender: string;
  received_at: string | null;
  /**
   * the few lines that support the follow-up (≤ 400 chars)
   */
  excerpt: string;
  web_link: string | null;
}
/**
 * A reply or follow-up ALFRED drafted. ATLAS never sends email.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "EmailDraft".
 */
export interface EmailDraft {
  id: string;
  node: string;
  followup_id: string | null;
  to: string[];
  cc: string[];
  subject: string;
  body: string;
  /**
   * source message id
   */
  in_reply_to: string | null;
  status: "PROPOSED" | "APPROVED" | "DISCARDED" | "EXPORTED";
  export: ("eml" | "outlook_drafts") | null;
  download_url: string | null;
  created_at: string;
  updated_at: string;
}
/**
 * A CC briefing produced by an inbox scan (docs/INBOX.md §4).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Digest".
 */
export interface Digest {
  id: string;
  node: string;
  mission_id: string | null;
  window_start: string | null;
  window_end: string | null;
  /**
   * ≤ 3 lines: what matters most
   */
  headline: string[];
  threads: DigestThread[];
  /**
   * CC emails excluded by rules or as automated
   */
  skipped: number;
  created_at: string;
}
/**
 * One conversation where the user is only in CC, summarized.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "DigestThread".
 */
export interface DigestThread {
  conversation_id: string;
  subject: string;
  /**
   * from the user's local project keywords
   */
  project: string | null;
  participants: string[];
  /**
   * 3-5 bullets: what happened
   */
  summary: string[];
  decisions: string[];
  /**
   * key numbers with their context
   */
  figures: string[];
  /**
   * what someone asked the user, if anything
   */
  asks_me: string | null;
  importance: Priority3;
  messages: EmailRef[];
  followup_id: string | null;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Alert".
 */
export interface Alert {
  id: string;
  node: string;
  /**
   * which check raised it: dashboards | l10 | rocks | …
   */
  check: string;
  kind:
    | "missing_report"
    | "identical_report"
    | "moved_date"
    | "removed_row"
    | "kpi_mismatch"
    | "value_change"
    | "overdue_todo"
    | "unreported_todo"
    | "stale_issue"
    | "rock_failed"
    | "rock_at_risk"
    | "other";
  severity: "LOW" | "MEDIUM" | "HIGH";
  project: string | null;
  title: string;
  detail: string;
  evidence: AlertEvidence[];
  /**
   * stable key so the same finding updates instead of duplicating
   */
  fingerprint: string;
  status: "OPEN" | "ACKNOWLEDGED" | "RESOLVED";
  first_seen: string;
  last_seen: string;
  mission_id: string | null;
}
/**
 * A verbatim line from a source, so every alert can be checked.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "AlertEvidence".
 */
export interface AlertEvidence {
  /**
   * file name, API resource or email subject
   */
  source: string;
  version: "previous" | "current" | "single";
  /**
   * verbatim text (≤ 300 chars)
   */
  quote: string;
}
/**
 * A Rock tracked by ARGOS from the user's private rocks file.
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "RockStatus".
 */
export interface RockStatus {
  id: string;
  title: string;
  owner: string;
  project: string | null;
  quarter: string;
  due: string;
  /**
   * e.g. 'unidades escrituradas'
   */
  metric: string | null;
  target: number | null;
  current: number | null;
  start_value: number | null;
  start_date: string | null;
  status: "ON_TRACK" | "AT_RISK" | "OFF_TRACK" | "DONE" | "FAILED" | "UNKNOWN";
  reason: string;
  /**
   * units per week needed from today
   */
  required_pace: number | null;
  /**
   * units per week so far
   */
  observed_pace: number | null;
  updated_at: string;
}
/**
 * The weekly L10 brief ARGOS prepares (a deliverable plus a structured summary).
 *
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "Brief".
 */
export interface Brief {
  id: string;
  node: string;
  /**
   * ISO week, e.g. 2026-W40
   */
  week: string;
  headline: string[];
  sections: {
    [k: string]: string[];
  };
  deliverable: Attachment | null;
  mission_id: string | null;
  created_at: string;
}
/**
 * This interface was referenced by `AtlasContracts`'s JSON-Schema
 * via the `definition` "WorldState".
 */
export interface WorldState {
  nodes: NodeDefinition[];
  divisions: DivisionDefinition[];
  agents: AgentDefinition[];
  agent_states: AgentState[];
  missions: Mission[];
  tasks: Task[];
  messages: AgentMessage[];
  agent_reports: AgentReport[];
  mission_reports: MissionReport[];
  approvals: ApprovalRequest[];
  evidence: Evidence[];
  followups: FollowUp[];
  drafts: EmailDraft[];
  digests: Digest[];
  alerts: Alert[];
  rocks: RockStatus[];
  briefs: Brief[];
  last_seq: number;
}
