/**
 * Mock mode — a client-side, scripted event stream that follows docs/EVENTS.md exactly
 * (every event carries the FULL updated object). Lets the Command Center run without the API.
 *
 * Enable with NEXT_PUBLIC_ATLAS_MOCK=1 or ?mock=1 (optional ?speed=2 to accelerate).
 */
import type { AtlasConfig, Availability, Decision, LaunchMissionBody, Scenario } from "./api";
import type {
  AgentDefinition,
  AgentMessage,
  AgentReport,
  AgentState,
  AgentStatus,
  ApprovalReason,
  ApprovalRequest,
  AtlasEvent,
  Claim,
  DivisionDefinition,
  EventType,
  MessageType,
  Mission,
  MissionPhase,
  MissionReport,
  NodeDefinition,
  Priority,
  Task,
  TaskStatus,
  WorldState,
} from "./contracts";
import type { Transport } from "./store";

/* ---------------------------------------------------------------- organization (mirrors /agents) */

const NODES: NodeDefinition[] = [
  { id: "corporate", name: "Corporate", description: "Company work — strategy, finance, operations, EOS.", color: "#7dd3fc", enabled: true },
  { id: "personal", name: "Personal", description: "Personal matters. Fully isolated from Corporate.", color: "#f9a8d4", enabled: false },
];

const DIVISIONS: DivisionDefinition[] = [
  { id: "eos", name: "EOS", node: "corporate", description: "Entrepreneurial Operating System specialists — one per EOS component.", color: "#fb923c", lead: null },
];

function agent(p: Partial<AgentDefinition> & Pick<AgentDefinition, "id" | "name" | "title" | "description" | "color">): AgentDefinition {
  return {
    kind: "native",
    adapter: "mock",
    adapter_config: {},
    model: null,
    capabilities: [],
    tools: [],
    permissions: { can_delegate: false, can_message: ["*"], requires_approval_for: [], max_parallel_tasks: 3 },
    system_prompt: null,
    enabled: true,
    is_orchestrator: false,
    nodes: ["*"],
    division: null,
    ...p,
  };
}

const eos = (key: string, title: string, description: string) =>
  agent({ id: `eos-${key}`, name: `EOS·${key.toUpperCase()}`, title, description, color: "#fb923c", kind: "external", adapter: "claude_md", nodes: ["corporate"], division: "eos", capabilities: ["eos", key] });

const AGENTS: AgentDefinition[] = [
  agent({ id: "atlas", name: "ATLAS", title: "Orchestrator / Commander", color: "#e2e8f0", is_orchestrator: true, description: "Receives objectives, decomposes them into tasks, delegates to specialists, monitors progress, resolves conflicts and consolidates the final report.", capabilities: ["decomposition", "delegation", "supervision", "consolidation", "reporting"] }),
  agent({ id: "sofia", name: "SOFIA", title: "Research", color: "#38bdf8", description: "Investigates, finds sources, and builds context — markets, comparables, competitors, regulations.", capabilities: ["research", "source_discovery", "market_research", "summarization"] }),
  agent({ id: "argos", name: "ARGOS", title: "Monitor", color: "#fbbf24", description: "Watches sources, processes and events; detects changes, anomalies and inconsistencies and alerts ATLAS.", capabilities: ["monitoring", "change_detection", "anomaly_detection", "consistency_checks"] }),
  agent({ id: "oracle", name: "ORACLE", title: "Strategy & Analysis", color: "#a78bfa", description: "Analyzes what other agents gather, compares scenarios and builds recommendations — always separating facts, assumptions, scenarios and recommendations.", capabilities: ["analysis", "scenario_modeling", "financial_reasoning"] }),
  agent({ id: "alfred", name: "ALFRED", title: "Execution / Operations", color: "#34d399", description: "Turns approved decisions into concrete actions — deliverables, follow-ups and routine workflows. External actions always go through human approval.", capabilities: ["execution", "deliverables", "follow_up"] }),
  eos("vision", "Vision", "V/TO, Core Values, Core Focus, 10-Year Target, 3-Year Picture and 1-Year Plan."),
  eos("people", "People", "Accountability Chart, seats, GWC and Core Values."),
  eos("data", "Data", "Scorecard, measurables per seat, thresholds and data integrity."),
  eos("issues", "Issues", "Issues List and IDS quality — symptoms vs. root causes."),
  eos("process", "Process", "Core processes, documentation and standardization."),
  eos("traction", "Traction", "Quarterly Rocks, SMART quality, Level 10 meetings and cadence."),
];

const SCENARIOS: Scenario[] = [
  {
    id: "zapopan-site-acquisition",
    node: "corporate",
    title: "Residential site acquisition",
    objective:
      "Evaluate acquiring a 4,200 m² lot in Zapopan for a 120-unit vertical housing project and recommend go / no-go with an offer range.",
  },
];

/* ---------------------------------------------------------------- live-mode config (Phase 2) */

/** Mock has no API key, so LIVE is unavailable; it still reports models and context like /config would. */
const CONFIG: AtlasConfig = {
  live_available: false,
  models: { orchestrator: "claude-opus-5-5", default: "claude-sonnet-5" },
  web_search: false,
  context_nodes: ["corporate"],
};

/** One claude_md agent whose file is missing, to exercise the OFFLINE state (docs/LIVE.md § Agent sources). */
const AVAILABILITY: Availability = {
  "eos-process": { available: false, reason: "EOS agent file not found: ${EOS_AGENTS_DIR}/eos-process.md" },
};

/* ---------------------------------------------------------------- engine */

let uid = 0;
const rid = (p: string) => `${p}_${Date.now().toString(36)}${(uid++).toString(36)}`;
const now = () => new Date().toISOString();

type StepFn = (e: MockEngine) => void | "pause";
interface Step {
  wait: number; // seconds at speed 1
  run: StepFn;
}

interface TaskSpec {
  title: string;
  description: string;
  assigned_to: string;
  priority: Priority;
  depends_on?: string[];
  requires_approval?: boolean;
  approval_reason?: ApprovalReason;
}

class MockEngine {
  seq = 0;
  world: WorldState;
  listener: ((e: AtlasEvent) => void) | null = null;
  mission: Mission | null = null;
  tasks = new Map<string, Task>(); // by script ref
  queue: Step[] = [];
  timer: ReturnType<typeof setTimeout> | null = null;
  paused = false;
  pendingApproval: ApprovalRequest | null = null;

  constructor(public speed: number) {
    const ts = now();
    this.world = {
      nodes: NODES,
      divisions: DIVISIONS,
      agents: AGENTS,
      agent_states: AGENTS.map((a) => ({ agent_id: a.id, status: "IDLE", current_task_id: null, activity: null, collaborating_with: [], updated_at: ts })),
      missions: [],
      tasks: [],
      messages: [],
      agent_reports: [],
      mission_reports: [],
      approvals: [],
      last_seq: 0,
    };
  }

  snapshot(): WorldState {
    return structuredClone({ ...this.world, last_seq: this.seq });
  }

  emit(type: EventType, payload: Record<string, unknown>, summary: string, agent_id: string | null = null) {
    const e: AtlasEvent = { id: rid("evt"), seq: ++this.seq, type, mission_id: this.mission?.id ?? null, agent_id, summary, payload, ts: now() };
    this.listener?.(e);
  }

  /* --- scheduling */
  run() {
    if (this.timer || this.paused) return;
    const step = this.queue.shift();
    if (!step) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      const r = step.run(this);
      if (r === "pause") this.paused = true;
      else this.run();
    }, (step.wait * 1000) / this.speed);
  }
  push(steps: Step[]) {
    this.queue.push(...steps);
    this.run();
  }
  resume(steps: Step[]) {
    this.paused = false;
    this.queue.unshift(...steps);
    this.run();
  }
  stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.queue = [];
  }

  /* --- world helpers (always emit full objects) */
  phase(phase: MissionPhase) {
    if (!this.mission) return;
    this.mission = { ...this.mission, phase, closed_at: phase === "CLOSED" ? now() : null };
    this.emit(phase === "CLOSED" ? "mission.closed" : "mission.phase_changed", { mission: this.mission }, phase === "CLOSED" ? "Mission closed" : `Mission entered ${phase.replace("_", " ")}`, "atlas");
  }

  agent(id: string, status: AgentStatus, activity: string | null, taskRef?: string | null, collab: string[] = []) {
    const state: AgentState = {
      agent_id: id,
      status,
      current_task_id: taskRef ? this.tasks.get(taskRef)?.id ?? null : null,
      activity,
      collaborating_with: collab,
      updated_at: now(),
    };
    const name = AGENTS.find((a) => a.id === id)?.name ?? id;
    this.emit("agent.state_changed", { state }, `${name} → ${status}${activity ? ` · ${activity}` : ""}`, id);
  }

  task(ref: string, spec: TaskSpec) {
    if (!this.mission) return;
    const t: Task = {
      id: rid("tsk"),
      mission_id: this.mission.id,
      title: spec.title,
      description: spec.description,
      assigned_to: spec.assigned_to,
      created_by: "atlas",
      status: spec.depends_on?.length ? "PENDING" : "READY",
      priority: spec.priority,
      depends_on: (spec.depends_on ?? []).map((r) => this.tasks.get(r)!.id),
      requires_approval: spec.requires_approval ?? false,
      approval_reason: spec.approval_reason ?? null,
      progress: 0,
      parent_task_id: null,
      result_report_id: null,
      created_at: now(),
      started_at: null,
      completed_at: null,
    };
    this.tasks.set(ref, t);
    this.mission = { ...this.mission, task_ids: [...this.mission.task_ids, t.id] };
    const who = AGENTS.find((a) => a.id === spec.assigned_to)?.name ?? spec.assigned_to;
    this.emit("task.created", { task: t }, `ATLAS created '${t.title}' → ${who}`, "atlas");
  }

  tupd(ref: string, status?: TaskStatus, progress?: number, extra: Partial<Task> = {}) {
    const prev = this.tasks.get(ref);
    if (!prev) return;
    const t: Task = {
      ...prev,
      ...extra,
      status: status ?? prev.status,
      progress: progress ?? prev.progress,
      started_at: prev.started_at ?? (status === "IN_PROGRESS" ? now() : null),
      completed_at: status === "COMPLETED" ? now() : prev.completed_at,
    };
    this.tasks.set(ref, t);
    const label =
      status && status !== prev.status
        ? `'${t.title}' → ${status.replace("_", " ")}`
        : `'${t.title}' at ${Math.round(t.progress * 100)}%`;
    this.emit("task.updated", { task: t }, label, t.assigned_to);
  }

  msg(from: string, to: string, type: MessageType, subject: string, body: string, taskRef?: string, requires_response = false) {
    if (!this.mission) return;
    const m: AgentMessage = {
      id: rid("msg"),
      mission_id: this.mission.id,
      task_id: taskRef ? this.tasks.get(taskRef)?.id ?? null : null,
      from,
      to,
      type,
      subject,
      body,
      attachments: [],
      confidence: type === "RESULT" ? "HIGH" : null,
      requires_response,
      in_reply_to: null,
      created_at: now(),
    };
    const n = (id: string) => AGENTS.find((a) => a.id === id)?.name ?? id;
    this.emit("message.sent", { message: m }, `${n(from)} → ${n(to)} · ${type}: ${subject}`, from);
  }

  report(agentId: string, taskRef: string, r: Omit<AgentReport, "id" | "mission_id" | "task_id" | "agent_id" | "created_at" | "attachments">) {
    if (!this.mission) return;
    const t = this.tasks.get(taskRef)!;
    const report: AgentReport = { id: rid("rpt"), mission_id: this.mission.id, task_id: t.id, agent_id: agentId, created_at: now(), attachments: [], ...r };
    this.tasks.set(taskRef, { ...t, result_report_id: report.id });
    const name = AGENTS.find((a) => a.id === agentId)?.name ?? agentId;
    this.emit("report.submitted", { report }, `${name} submitted report for '${t.title}'`, agentId);
    return report;
  }

  log(summary: string, agentId: string | null = "atlas") {
    this.emit("log", {}, summary, agentId);
  }
}

/* ---------------------------------------------------------------- the scripted mission */

const C = (kind: Claim["kind"], statement: string, confidence: Claim["confidence"] = "MEDIUM", sources: string[] = []): Claim => ({ kind, statement, confidence, sources });

const s = (wait: number, run: StepFn): Step => ({ wait, run });

function scriptOpening(): Step[] {
  return [
    s(0.8, (e) => { e.phase("DECOMPOSITION"); e.agent("atlas", "WORKING", "Decomposing objective into workstreams"); }),
    s(1.2, (e) => e.log("ATLAS identified 6 workstreams across 5 agents and 1 division")),
    s(0.5, (e) => e.task("market", { title: "Market study & comparables", description: "Comparable vertical projects within 3 km, price/m², absorption and unit mix.", assigned_to: "sofia", priority: "HIGH" })),
    s(0.3, (e) => e.task("zoning", { title: "Zoning & permit watch", description: "Land-use plan, density (CUS/COS), permits and municipal gazette changes.", assigned_to: "argos", priority: "MEDIUM" })),
    s(0.3, (e) => e.task("capacity", { title: "Team capacity check (Rocks)", description: "Can the organization absorb a new project this quarter? Rocks at risk.", assigned_to: "eos-traction", priority: "MEDIUM" })),
    s(0.3, (e) => e.task("model", { title: "Financial scenarios & offer range", description: "Base / density-cut / slow-absorption scenarios; residual land value; offer range.", assigned_to: "oracle", priority: "HIGH", depends_on: ["market"] })),
    s(0.3, (e) => e.task("loi", { title: "Draft & send non-binding LOI", description: "Prepare a non-binding letter of intent to the seller's broker within the approved range.", assigned_to: "alfred", priority: "CRITICAL", depends_on: ["model"], requires_approval: true, approval_reason: "EXTERNAL_COMMUNICATION" })),
    s(0.3, (e) => e.task("consolidate", { title: "Consolidate mission report", description: "Merge agent reports, resolve conflicts, flag assumptions and human decisions.", assigned_to: "atlas", priority: "HIGH", depends_on: ["market", "zoning", "capacity", "model", "loi"] })),
    s(0.9, (e) => { e.phase("DELEGATION"); e.agent("atlas", "WORKING", "Delegating workstreams to specialists"); }),
    s(0.7, (e) => e.msg("atlas", "sofia", "REQUEST", "Market study: Zapopan vertical housing", "Build a comparables set within 3 km: price/m², absorption, unit mix, amenities.", "market", true)),
    s(0.5, (e) => e.msg("atlas", "argos", "REQUEST", "Watch zoning & permits for the lot", "Monitor land-use plan, density coefficients and the municipal gazette.", "zoning", true)),
    s(0.5, (e) => e.msg("atlas", "eos-traction", "REQUEST", "Capacity check for a new project", "Review Q3 Rocks and L10 cadence: can we absorb this without Rocks at risk?", "capacity", true)),
    s(0.4, (e) => e.agent("oracle", "WAITING", "Waiting on SOFIA's market dataset", "model")),
    s(0.9, (e) => {
      e.phase("EXECUTION");
      e.agent("sofia", "WORKING", "Pulling comparable projects within 3 km", "market");
      e.tupd("market", "IN_PROGRESS", 0.08);
    }),
    s(0.5, (e) => { e.agent("argos", "MONITORING", "Watching Zapopan land-use plan & gazette", "zoning"); e.tupd("zoning", "IN_PROGRESS", 0.1); }),
    s(0.5, (e) => { e.agent("eos-traction", "WORKING", "Reviewing Q3 Rocks and L10 cadence", "capacity"); e.tupd("capacity", "IN_PROGRESS", 0.15); }),
    s(0.5, (e) => e.agent("atlas", "WAITING", "Supervising 3 active workstreams")),
    s(1.2, (e) => e.tupd("market", undefined, 0.3)),
    s(0.8, (e) => e.tupd("capacity", undefined, 0.45)),
    s(0.8, (e) => e.tupd("zoning", undefined, 0.35)),
    s(1.0, (e) => {
      e.msg("argos", "atlas", "ALERT", "Land-use amendment under public consultation", "Proposed amendment could cut max density (CUS) on the lot's corridor by ~20%. Consultation closes in 45 days.", "zoning");
      e.agent("argos", "COLLABORATING", "Escalated density-change alert to ATLAS", "zoning", ["atlas"]);
    }),
    s(0.8, (e) => { e.agent("atlas", "REVIEWING", "Assessing impact of density alert"); e.tupd("market", undefined, 0.5); }),
    s(1.0, (e) => {
      e.phase("COLLABORATION");
      e.msg("atlas", "sofia", "QUESTION", "Does the density change affect comparables?", "Flag comps in the amended corridor separately.", "market", true);
    }),
    s(0.7, (e) => {
      e.agent("sofia", "COLLABORATING", "Cross-checking comps against ARGOS's corridor map", "market", ["argos"]);
      e.msg("sofia", "argos", "QUESTION", "Which parcels fall in the amended corridor?", "Need the polygon to tag comparables.", "market", true);
    }),
    s(0.8, (e) => e.msg("argos", "sofia", "ANSWER", "Corridor polygon shared", "4 of 14 comps fall inside the amended corridor.", "market")),
    s(0.6, (e) => { e.agent("argos", "MONITORING", "Watching consultation docket", "zoning"); e.tupd("zoning", undefined, 0.7); }),
    s(0.8, (e) => { e.tupd("capacity", undefined, 0.9); e.tupd("market", undefined, 0.78); }),
    s(0.8, (e) => {
      e.tupd("capacity", "COMPLETED", 1);
      e.report("eos-traction", "capacity", {
        asked_to: "Assess whether the team can absorb a new development this quarter.",
        actions_taken: ["Reviewed 7 Q3 Rocks", "Checked L10 attendance & issue closure rate"],
        inputs_used: ["Q3 Rocks sheet", "L10 meeting notes"],
        findings: [
          C("FACT", "2 of 7 company Rocks are off-track (permits for Tower B; CRM rollout).", "HIGH", ["Q3 Rocks sheet"]),
          C("ASSUMPTION", "Development team can absorb due diligence if CRM rollout slips to Q4.", "MEDIUM"),
          C("RECOMMENDATION", "Assign a single owner for the acquisition as a new Q4 Rock.", "MEDIUM"),
        ],
        unresolved: ["Integrator sign-off on moving the CRM Rock"],
        needs_agents: [],
        confidence: "MEDIUM",
        limitations: ["No visibility on individual seat workload"],
      });
      e.msg("eos-traction", "atlas", "RESULT", "Capacity: feasible with one Rock moved", "2/7 Rocks off-track; recommend a dedicated Q4 Rock owner.", "capacity");
      e.agent("eos-traction", "COMPLETED", "Capacity report delivered");
    }),
    s(1.0, (e) => {
      e.tupd("market", "COMPLETED", 1);
      e.report("sofia", "market", {
        asked_to: "Build a comparables set and market read for vertical housing near the lot.",
        actions_taken: ["Collected 14 comparable projects within 3 km", "Normalized price/m² by delivery date", "Tagged 4 comps inside the amended corridor"],
        inputs_used: ["Listing portals", "Developer brochures", "ARGOS corridor polygon"],
        findings: [
          C("FACT", "Median asking price is MXN 58,400/m² across 14 comparable projects.", "HIGH", ["14 listings"]),
          C("FACT", "Average absorption is 3.1 units/month per project over the last 12 months.", "MEDIUM", ["Developer sales reports"]),
          C("ASSUMPTION", "2BR units of 65–75 m² remain the demand sweet spot through 2028.", "MEDIUM"),
        ],
        unresolved: ["Closing prices vs. asking prices (discount not observed)"],
        needs_agents: ["oracle"],
        confidence: "HIGH",
        limitations: ["Asking prices only; no notarized transactions"],
      });
      e.msg("sofia", "oracle", "RESULT", "Market dataset: 14 comps, 3.1 u/mo absorption", "Median MXN 58.4k/m²; 2BR 65–75 m² sweet spot. 4 comps in amended corridor flagged.", "market");
      e.agent("sofia", "COMPLETED", "Market study delivered to ORACLE");
    }),
    s(0.8, (e) => {
      e.tupd("model", "IN_PROGRESS", 0.1);
      e.agent("oracle", "WORKING", "Modeling base / density-cut / slow-absorption scenarios", "model");
      e.agent("atlas", "WAITING", "Waiting on ORACLE's scenarios");
    }),
    s(1.2, (e) => e.tupd("model", undefined, 0.4)),
    s(0.8, (e) => {
      e.agent("oracle", "COLLABORATING", "Confirming 2BR price point with SOFIA", "model", ["sofia"]);
      e.msg("oracle", "sofia", "QUESTION", "Confirm price/m² for 2BR units", "Is the 2BR premium above the median?", "model", true);
    }),
    s(0.8, (e) => {
      e.msg("sofia", "oracle", "ANSWER", "2BR premium ≈ +4%", "MXN 60.7k/m² for 2BR vs 58.4k median.", "model");
      e.agent("oracle", "WORKING", "Computing residual land value per scenario", "model");
    }),
    s(1.0, (e) => { e.tupd("model", undefined, 0.75); e.tupd("zoning", undefined, 0.9); }),
    s(0.8, (e) => {
      e.tupd("zoning", "COMPLETED", 1);
      e.report("argos", "zoning", {
        asked_to: "Monitor zoning, density and permits affecting the lot.",
        actions_taken: ["Scanned municipal gazette (90 days)", "Mapped amended corridor", "Set watch on consultation docket"],
        inputs_used: ["Zapopan municipal gazette", "Partial land-use plan"],
        findings: [
          C("FACT", "A land-use amendment affecting the lot's corridor is under public consultation until mid-November.", "HIGH", ["Municipal gazette"]),
          C("SCENARIO", "If approved as drafted, max buildable area drops ~20% (≈ 96 units instead of 120).", "MEDIUM"),
        ],
        unresolved: ["Final text of the amendment"],
        needs_agents: ["oracle"],
        confidence: "HIGH",
        limitations: ["Consultation outcome cannot be predicted"],
      });
      e.agent("argos", "MONITORING", "Continuous watch on consultation docket", null);
    }),
    s(0.9, (e) => {
      e.tupd("model", "COMPLETED", 1);
      e.report("oracle", "model", {
        asked_to: "Model scenarios and recommend an offer range.",
        actions_taken: ["Built 3-scenario residual land value model", "Stress-tested absorption at 2.2 u/mo"],
        inputs_used: ["SOFIA market dataset", "ARGOS density alert", "Construction cost index Q3"],
        findings: [
          C("SCENARIO", "Base case (120 units): residual land value ≈ MXN 64M, IRR 21%.", "MEDIUM"),
          C("SCENARIO", "Density cut (96 units): residual land value ≈ MXN 51M, IRR 15%.", "MEDIUM"),
          C("ASSUMPTION", "Construction cost MXN 19.5k/m² holds within ±5% through 2027.", "LOW"),
          C("RECOMMENDATION", "GO with a non-binding offer of MXN 56M, conditioned on the zoning outcome.", "MEDIUM"),
        ],
        unresolved: ["Seller's flexibility on a zoning condition precedent"],
        needs_agents: ["alfred"],
        confidence: "MEDIUM",
        limitations: ["Asking-price comps only", "No soil study"],
      });
      e.msg("oracle", "atlas", "RESULT", "Recommendation: GO at ≤ MXN 56M, zoning-conditioned", "Base IRR 21%; density-cut IRR 15%. Offer 56M with condition precedent.", "model");
      e.agent("oracle", "COMPLETED", "Scenarios delivered");
    }),
    s(1.0, (e) => {
      e.phase("VALIDATION");
      e.agent("atlas", "REVIEWING", "Cross-checking ORACLE's model against ARGOS alert");
    }),
    s(1.0, (e) => e.msg("atlas", "oracle", "REVIEW", "Validated with caveat", "Model consistent with SOFIA data; density risk must be a condition precedent.", "model")),
    s(0.8, (e) => {
      e.msg("atlas", "alfred", "REQUEST", "Draft non-binding LOI at MXN 56M", "Include zoning condition precedent and 60-day due diligence window.", "loi", true);
      e.tupd("loi", "IN_PROGRESS", 0.2);
      e.agent("alfred", "WORKING", "Drafting LOI with zoning condition precedent", "loi");
    }),
    s(1.2, (e) => e.tupd("loi", undefined, 0.6)),
    s(1.0, (e) => {
      e.tupd("loi", "AWAITING_APPROVAL", 0.8);
      const a: ApprovalRequest = {
        id: rid("apr"),
        mission_id: e.mission!.id,
        task_id: e.tasks.get("loi")!.id,
        requested_by: "alfred",
        reason: "EXTERNAL_COMMUNICATION",
        title: "Send non-binding LOI to seller's broker",
        detail: "ALFRED drafted a non-binding letter of intent for MXN 56M with a zoning condition precedent and a 60-day due diligence window. Sending it is an external communication on behalf of the company.",
        proposed_action: "Email LOI (PDF) to the seller's broker and log it in the CRM.",
        options: ["APPROVED", "REJECTED"],
        state: "PENDING",
        decision_note: null,
        created_at: now(),
        decided_at: null,
      };
      e.pendingApproval = a;
      e.emit("approval.requested", { approval: a }, `ALFRED requests approval: ${a.title}`, "alfred");
      e.agent("alfred", "BLOCKED", "Awaiting human approval to send LOI", "loi");
      e.agent("atlas", "WAITING", "Paused — human decision required");
      return "pause";
    }),
  ];
}

function scriptAfterDecision(decision: Decision): Step[] {
  const approved = decision === "APPROVED";
  const steps: Step[] = approved
    ? [
        s(0.6, (e) => { e.agent("alfred", "WORKING", "Sending LOI to seller's broker", "loi"); e.tupd("loi", "IN_PROGRESS", 0.9); }),
        s(1.0, (e) => {
          e.tupd("loi", "COMPLETED", 1);
          e.report("alfred", "loi", {
            asked_to: "Draft and send a non-binding LOI within the approved range.",
            actions_taken: ["Drafted LOI (MXN 56M, zoning CP, 60-day DD)", "Sent to seller's broker after human approval", "Logged in CRM"],
            inputs_used: ["ORACLE recommendation", "Human approval"],
            findings: [C("FACT", "LOI delivered to the seller's broker; response requested within 10 business days.", "HIGH")],
            unresolved: ["Seller response"],
            needs_agents: [],
            confidence: "HIGH",
            limitations: [],
          });
          e.msg("alfred", "atlas", "RESULT", "LOI sent", "Delivered to broker; follow-up scheduled in 10 business days.", "loi");
          e.agent("alfred", "COMPLETED", "LOI sent · follow-up scheduled");
        }),
      ]
    : [
        s(0.6, (e) => {
          e.tupd("loi", "CANCELLED", 0.8);
          e.report("alfred", "loi", {
            asked_to: "Draft and send a non-binding LOI within the approved range.",
            actions_taken: ["Drafted LOI (MXN 56M, zoning CP, 60-day DD)", "Held — human rejected sending"],
            inputs_used: ["ORACLE recommendation", "Human decision"],
            findings: [C("FACT", "LOI drafted but not sent per human decision.", "HIGH")],
            unresolved: ["Revised terms from leadership"],
            needs_agents: [],
            confidence: "HIGH",
            limitations: [],
          });
          e.msg("alfred", "atlas", "RESULT", "LOI held", "Draft kept on file; nothing sent externally.", "loi");
          e.agent("alfred", "COMPLETED", "LOI held per human decision");
        }),
      ];

  return [
    ...steps,
    s(0.8, (e) => {
      e.phase("CONSOLIDATION");
      e.agent("atlas", "WORKING", "Consolidating 5 agent reports", "consolidate");
      e.tupd("consolidate", "IN_PROGRESS", 0.3);
    }),
    s(1.2, (e) => e.tupd("consolidate", undefined, 0.7)),
    s(1.0, (e) => {
      e.phase("REPORTING");
      e.tupd("consolidate", "COMPLETED", 1);
      const m = e.mission!;
      const report: MissionReport = {
        id: rid("rpt"),
        mission_id: m.id,
        executive_summary: approved
          ? "The Zapopan lot is attractive at the right price. Market fundamentals are solid (MXN 58.4k/m², 3.1 units/month), and the base case supports a residual land value of ≈ MXN 64M. A pending land-use amendment could cut density ~20%, so ATLAS recommends GO at MXN 56M with a zoning condition precedent. The non-binding LOI was approved and sent."
          : "The Zapopan lot is attractive at the right price, but a pending land-use amendment could cut density ~20%. ATLAS recommends GO at MXN 56M with a zoning condition precedent. Sending the LOI was rejected by the human reviewer; the draft is on file awaiting revised terms.",
        objective_status: approved ? "ACHIEVED" : "PARTIAL",
        tasks_completed: [...e.tasks.values()].filter((t) => t.status === "COMPLETED").map((t) => t.id),
        tasks_pending: [...e.tasks.values()].filter((t) => t.status !== "COMPLETED").map((t) => t.id),
        key_findings: [
          C("FACT", "Median asking price is MXN 58,400/m² across 14 comparable projects (SOFIA).", "HIGH"),
          C("FACT", "A land-use amendment on the lot's corridor is under public consultation (ARGOS).", "HIGH"),
          C("FACT", "2 of 7 company Rocks are off-track this quarter (EOS·TRACTION).", "HIGH"),
          C("ASSUMPTION", "Construction cost holds within ±5% through 2027.", "LOW"),
          C("SCENARIO", "Base case: 120 units, RLV ≈ MXN 64M, IRR 21%.", "MEDIUM"),
          C("SCENARIO", "Density cut: 96 units, RLV ≈ MXN 51M, IRR 15%.", "MEDIUM"),
          C("RECOMMENDATION", "GO with a non-binding offer of MXN 56M conditioned on the zoning outcome.", "MEDIUM"),
        ],
        conflicts: ["ORACLE's base case assumes 120 units while ARGOS flags a possible 96-unit cap — resolved by making zoning a condition precedent."],
        assumptions: ["Asking prices approximate closing prices", "Construction cost ±5% through 2027", "2BR 65–75 m² remains the demand sweet spot"],
        needs_human_attention: approved
          ? ["Integrator sign-off on moving the CRM Rock to Q4", "Commission a soil study before binding offer"]
          : ["Decide revised LOI terms", "Integrator sign-off on moving the CRM Rock to Q4"],
        next_actions: [
          "ARGOS keeps watching the consultation docket and alerts on any change",
          approved ? "ALFRED follows up with the broker in 10 business days" : "ALFRED holds the LOI draft until new terms",
          "Create Q4 Rock: 'Zapopan acquisition due diligence' with a single owner",
        ],
        references: [],
        agent_report_ids: [],
        created_at: now(),
      };
      e.mission = { ...m, final_report_id: report.id };
      e.emit("mission.report_ready", { report }, "ATLAS published the mission report", "atlas");
      e.agent("atlas", "REVIEWING", "Presenting mission report");
    }),
    s(1.4, (e) => {
      e.phase("FOLLOW_UP");
      e.agent("argos", "MONITORING", "Follow-up: watching land-use consultation outcome");
      e.agent("sofia", "IDLE", null);
      e.agent("oracle", "IDLE", null);
      e.agent("eos-traction", "IDLE", null);
    }),
    s(1.4, (e) => {
      e.phase("CLOSED");
      e.agent("atlas", "COMPLETED", "Mission closed · report delivered");
      e.agent("alfred", "IDLE", null);
    }),
  ];
}

/* ---------------------------------------------------------------- transport */

export function mockTransport({ speed = 1 }: { speed?: number } = {}): Transport {
  const engine = new MockEngine(speed);
  return {
    mode: "mock",
    connect(h) {
      engine.listener = h.onEvent;
      h.onSnapshot(engine.snapshot());
      h.onStatus("mock");
      return () => {
        engine.listener = null;
        engine.stop();
      };
    },
    scenarios: async () => SCENARIOS,
    config: async () => CONFIG,
    availability: async () => AVAILABILITY,
    async cancel(id: string) {
      const m = engine.mission;
      if (!m || m.id !== id) throw new Error("ATLAS API 404: mission not found");
      if (m.phase === "CLOSED") return m;
      engine.stop();
      engine.paused = false;
      for (const [ref, t] of engine.tasks) {
        if (!["COMPLETED", "FAILED", "CANCELLED"].includes(t.status)) engine.tupd(ref, "CANCELLED");
      }
      const a = engine.pendingApproval;
      if (a) {
        engine.pendingApproval = null;
        const expired: ApprovalRequest = { ...a, state: "EXPIRED", decision_note: "Mission cancelled", decided_at: now() };
        engine.emit("approval.decided", { approval: expired }, `Approval expired: ${a.title}`, null);
      }
      for (const ag of AGENTS) engine.agent(ag.id, "IDLE", null);
      engine.mission = { ...engine.mission!, phase: "CLOSED", closed_at: now() };
      engine.emit("mission.closed", { mission: engine.mission }, "Mission cancelled by human", "atlas");
      return engine.mission;
    },
    async launch(body: LaunchMissionBody) {
      if (body.mode === "live") throw new Error("ATLAS API 422: live mode requires ANTHROPIC_API_KEY");
      engine.stop();
      engine.paused = false;
      engine.tasks.clear();
      const scenario = SCENARIOS.find((x) => x.id === body.scenario_id) ?? SCENARIOS[0];
      const mission: Mission = {
        id: rid("msn"),
        objective: body.objective?.trim() || scenario.objective,
        node: body.node,
        mode: "simulated",
        usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 },
        context: scenario.title,
        phase: "OBJECTIVE",
        priority: "HIGH",
        task_ids: [],
        final_report_id: null,
        created_at: now(),
        closed_at: null,
      };
      engine.mission = mission;
      engine.emit("mission.created", { mission }, `Mission received: ${mission.objective}`, "atlas");
      engine.agent("atlas", "WORKING", "Analyzing objective");
      engine.push(scriptOpening());
      return mission;
    },
    async decide(id: string, decision: Decision, note?: string) {
      const a = engine.pendingApproval;
      if (!a || a.id !== id) throw new Error("Approval not pending");
      const decided: ApprovalRequest = { ...a, state: decision, decision_note: note ?? null, decided_at: now() };
      engine.pendingApproval = null;
      engine.emit("approval.decided", { approval: decided }, `Human ${decision.toLowerCase()}: ${a.title}`, null);
      engine.resume(scriptAfterDecision(decision));
      return decided;
    },
  };
}
