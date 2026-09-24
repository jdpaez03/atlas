/**
 * Mock mode — a client-side, scripted event stream that follows docs/EVENTS.md exactly
 * (every event carries the FULL updated object). Lets the Command Center run without the API.
 *
 * Enable with NEXT_PUBLIC_ATLAS_MOCK=1 or ?mock=1 (optional ?speed=2 to accelerate).
 */
import type { AtlasConfig, Availability, Decision, LaunchMissionBody, MissionSummary, Scenario } from "./api";
import type {
  AgentDefinition,
  AgentMessage,
  AgentReport,
  AgentState,
  AgentStatus,
  ApprovalReason,
  ApprovalRequest,
  AtlasEvent,
  Attachment,
  Claim,
  DivisionDefinition,
  EventType,
  Evidence,
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
import { mockInbox, type MockInbox } from "./mockInbox";
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
  agent({ id: "hermes", name: "HERMES", title: "Inbox", color: "#f0abfc", nodes: ["corporate"], description: "Reads the work mailbox and extracts commitments, requests and pending replies as follow-ups — precisely, without speculation.", capabilities: ["email", "follow_up", "extraction"] }),
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

/**
 * Mock "live" runs the same script with mode=live (fake usage) so the mission thread and follow-up rounds
 * can be exercised without an API key. `?live=0` turns it off to show the "Live agents off" hint.
 */
function mockConfig(): AtlasConfig {
  const off = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("live") === "0";
  return {
    live_available: !off,
    backend: off ? null : "api",
    cost_basis: off ? null : "api",
    models: { orchestrator: "claude-opus-5-5", default: "claude-sonnet-5" },
    web_search: !off,
    context_nodes: ["corporate"],
  };
}

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
  /** every mission this session knows (seeded history + launched), for GET /missions */
  missions = new Map<string, Mission>();
  reportVersions = new Map<string, number>();
  evidence: Evidence[] = [];
  deliverables: Attachment[] = [];
  lastReport: MissionReport | null = null;
  lastApprovalId: string | null = null;
  inbox: MockInbox;

  publishReport(report: MissionReport) {
    const m = this.mission!;
    this.lastReport = report;
    this.reportVersions.set(m.id, report.version);
    this.mission = { ...m, final_report_id: report.id };
    this.missions.set(m.id, this.mission);
    this.emit("mission.report_ready", { report }, report.version > 1 ? `ATLAS published mission report v${report.version}` : "ATLAS published the mission report", "atlas");
  }

  constructor(public speed: number) {
    const ts = now();
    const seed = seedHistory();
    for (const m of seed.missions) this.missions.set(m.id, m);
    this.inbox = mockInbox((type, payload, summary, agentId) => this.emit(type, payload, summary, agentId, null), speed);
    this.world = {
      nodes: NODES,
      divisions: DIVISIONS,
      agents: AGENTS,
      agent_states: AGENTS.map((a) => ({ agent_id: a.id, status: "IDLE", current_task_id: null, activity: null, collaborating_with: [], updated_at: ts })),
      missions: seed.missions,
      tasks: seed.tasks,
      messages: [],
      agent_reports: [],
      mission_reports: [],
      approvals: [],
      evidence: [],
      followups: this.inbox.followups,
      drafts: this.inbox.drafts,
      digests: this.inbox.digests,
      last_seq: 0,
    };
  }

  /** Keep the mission index in sync and emit it. */
  setMission(m: Mission, type: EventType, summary: string) {
    this.mission = m;
    this.missions.set(m.id, m);
    this.emit(type, { mission: m }, summary, "atlas");
  }

  /** Fake LLM usage for mock-live missions. */
  tick(calls = 1) {
    const m = this.mission;
    if (!m || m.mode !== "live") return;
    const u = m.usage;
    const inp = 5200 * calls, out = 900 * calls;
    const usage = {
      input_tokens: u.input_tokens + inp,
      output_tokens: u.output_tokens + out,
      cache_read_tokens: u.cache_read_tokens + 3100 * calls,
      llm_calls: u.llm_calls + calls,
      est_cost_usd: +(u.est_cost_usd + (inp * 3 + out * 15) / 1e6).toFixed(4),
    };
    this.mission = { ...m, usage };
    this.missions.set(m.id, this.mission);
    this.emit("mission.updated", { mission: this.mission }, "Usage updated", "atlas");
  }

  /** A system-recorded action (docs/PHASE3.md §A). */
  ev(agentId: string, taskRef: string | null, kind: Evidence["kind"], ref: string, detail: string, ok = true) {
    if (!this.mission) return;
    const ev: Evidence = {
      id: rid("evd"),
      mission_id: this.mission.id,
      task_id: taskRef ? this.tasks.get(taskRef)?.id ?? null : null,
      agent_id: agentId,
      kind,
      ref,
      detail,
      ok,
      at: now(),
    };
    this.evidence.push(ev);
    const name = AGENTS.find((a) => a.id === agentId)?.name ?? agentId;
    const verb: Record<Evidence["kind"], string> = {
      file_listed: "listed",
      file_read: "read",
      file_written: "wrote",
      web_search: "searched the web for",
      web_fetch: "fetched",
      consult: "consulted",
      approval: "requested approval",
      email_read: "read the email",
      draft_created: "drafted",
    };
    const short = ref.split(/[\\/]/).pop() || ref;
    this.emit("evidence.recorded", { evidence: ev }, `${name} ${verb[kind]} ${kind === "consult" ? short.toUpperCase() : short}${ok ? "" : ` — failed: ${detail}`}`, agentId);
    return ev;
  }

  /** A deliverable written to outputs/ — in mock it's a blob URL so the link really downloads. */
  deliverable(name: string, content: string, mime = "text/markdown"): Attachment {
    const url = typeof URL !== "undefined" && typeof Blob !== "undefined" ? URL.createObjectURL(new Blob([content], { type: mime })) : null;
    const a: Attachment = { id: rid("att"), name, kind: "file", uri: null, content: null, size_bytes: new Blob([content]).size, download_url: url };
    this.deliverables.push(a);
    return a;
  }

  snapshot(): WorldState {
    return structuredClone({ ...this.world, last_seq: this.seq });
  }

  emit(type: EventType, payload: Record<string, unknown>, summary: string, agent_id: string | null = null, missionId?: string | null) {
    const e: AtlasEvent = { id: rid("evt"), seq: ++this.seq, type, mission_id: missionId !== undefined ? missionId : this.mission?.id ?? null, agent_id, summary, payload, ts: now() };
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
    this.tick();
    const m = { ...this.mission, phase, closed_at: phase === "CLOSED" ? now() : null };
    this.setMission(m, phase === "CLOSED" ? "mission.closed" : "mission.phase_changed", phase === "CLOSED" ? "Mission closed" : `Mission entered ${phase.replace("_", " ")}`);
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
      round: this.mission.round,
      created_at: now(),
      started_at: null,
      completed_at: null,
    };
    this.tasks.set(ref, t);
    this.mission = { ...this.mission, task_ids: [...this.mission.task_ids, t.id] };
    this.missions.set(this.mission.id, this.mission);
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

  report(
    agentId: string,
    taskRef: string,
    r: Omit<AgentReport, "id" | "mission_id" | "task_id" | "agent_id" | "created_at" | "attachments" | "evidence" | "deliverables"> & { deliverables?: Attachment[] },
  ) {
    if (!this.mission) return;
    this.tick();
    const t = this.tasks.get(taskRef)!;
    // The runtime attaches the task's system-recorded evidence (never the agent's own account).
    const evidence = this.evidence.filter((x) => x.task_id === t.id);
    const report: AgentReport = { id: rid("rpt"), mission_id: this.mission.id, task_id: t.id, agent_id: agentId, created_at: now(), attachments: [], evidence, ...r, deliverables: r.deliverables ?? [] };
    this.tasks.set(taskRef, { ...t, result_report_id: report.id });
    const name = AGENTS.find((a) => a.id === agentId)?.name ?? agentId;
    this.emit("report.submitted", { report }, `${name} submitted report for '${t.title}'`, agentId);
    return report;
  }

  /** Mission thread message (human ↔ ATLAS). Works for any known mission, not only the running one. */
  thread(missionId: string, from: string, to: string, type: MessageType, subject: string, body: string) {
    const m: AgentMessage = {
      id: rid("msg"),
      mission_id: missionId,
      task_id: null,
      from,
      to,
      type,
      subject,
      body,
      attachments: [],
      confidence: null,
      requires_response: from === "human",
      in_reply_to: null,
      created_at: now(),
    };
    const e: AtlasEvent = {
      id: rid("evt"),
      seq: ++this.seq,
      type: "message.sent",
      mission_id: missionId,
      agent_id: from === "human" ? null : from,
      summary: from === "human" ? `Human → ATLAS: ${subject}` : `ATLAS → Human · ${type}: ${subject}`,
      payload: { message: m },
      ts: now(),
    };
    this.listener?.(e);
    return m;
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
      e.agent("sofia", "WORKING", "Searching the web: Zapopan vertical housing comparables", "market");
      e.tupd("market", "IN_PROGRESS", 0.08);
      e.ev("sofia", "market", "web_search", "Zapopan vertical housing comparables price per m2 2026", "9 results");
    }),
    s(0.5, (e) => { e.agent("argos", "MONITORING", "Watching Zapopan land-use plan & gazette", "zoning"); e.tupd("zoning", "IN_PROGRESS", 0.1); }),
    s(0.5, (e) => {
      const rocks = e.mission?.attachments.find((a) => /rock/i.test(a.name))?.name ?? "Rocks_Q3.xlsx";
      e.agent("eos-traction", "WORKING", `Reading ${rocks}`, "capacity");
      e.tupd("capacity", "IN_PROGRESS", 0.15);
      e.ev("eos-traction", "capacity", "file_read", `atlas-local/attachments/${e.mission!.id}/${rocks}`, "sheet 'Q3 Rocks' · 7 rows");
    }),
    s(0.5, (e) => e.agent("atlas", "WAITING", "Supervising 3 active workstreams")),
    s(1.2, (e) => {
      e.agent("sofia", "WORKING", "Reading Rocks_Q3.xlsx", "market");
      e.ev("sofia", "market", "file_listed", "~/Documents/Corporate/Zapopan", "14 entries");
      e.ev("sofia", "market", "file_read", "~/Documents/Corporate/Zapopan/Rocks_Q3.xlsx", "2 sheets · 7 rocks, 3 measurables");
      e.tupd("market", undefined, 0.3);
    }),
    s(0.8, (e) => {
      e.tupd("capacity", undefined, 0.45);
      e.ev("sofia", "market", "web_fetch", "https://www.inmuebles24.com/desarrollos/zapopan-vertical.html", "HTTP 403 — blocked by the site", false);
    }),
    s(0.6, (e) => {
      e.thread(e.mission!.id, "human", "atlas", "REQUEST", "Prioritize 2BR units", "Prioritize 2BR units in the market read, and use the Q3 Rocks file I attached for the capacity check.");
    }),
    s(1.0, (e) => {
      e.tick();
      e.thread(e.mission!.id, "atlas", "human", "ANSWER", "Noted", "Noted — SOFIA will weight 2BR comparables and EOS·TRACTION is already working from your Rocks_Q3.xlsx. Every task that starts from now on gets this guidance.");
    }),
    s(0.8, (e) => {
      e.tupd("zoning", undefined, 0.35);
      e.ev("argos", "zoning", "web_fetch", "https://www.zapopan.gob.mx/transparencia/gaceta-municipal/", "gazette index · last 90 days");
    }),
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
        inputs_used: ["Listing portals", "Rocks_Q3.xlsx", "Developer_sales_2025.pdf", "ARGOS corridor polygon"],
        findings: [
          C("FACT", "Median asking price is MXN 58,400/m² across 14 comparable projects.", "HIGH", ["14 listings"]),
          C("FACT", "Average absorption is 3.1 units/month per project over the last 12 months.", "MEDIUM", ["Developer sales reports"]),
          C("ASSUMPTION", "2BR units of 65–75 m² remain the demand sweet spot through 2028.", "MEDIUM"),
        ],
        unresolved: ["Closing prices vs. asking prices (discount not observed)"],
        needs_agents: ["oracle"],
        confidence: "LOW",
        limitations: ["Asking prices only; no notarized transactions", "Unverified: Developer_sales_2025.pdf (no system record)"],
      });
      e.msg("sofia", "oracle", "RESULT", "Market dataset: 14 comps, 3.1 u/mo absorption", "Median MXN 58.4k/m²; 2BR 65–75 m² sweet spot. 4 comps in amended corridor flagged.", "market");
      e.agent("sofia", "COMPLETED", "Market study delivered to ORACLE");
    }),
    s(0.8, (e) => {
      e.tupd("model", "IN_PROGRESS", 0.1);
      e.agent("oracle", "WORKING", "Modeling base / density-cut / slow-absorption scenarios", "model");
      e.agent("atlas", "WAITING", "Waiting on ORACLE's scenarios");
    }),
    s(1.2, (e) => {
      e.tupd("model", undefined, 0.4);
      e.ev("oracle", "model", "file_read", "~/Documents/Corporate/Finance/Construction_cost_index_Q3.pdf", "4 pages");
    }),
    s(0.8, (e) => {
      e.agent("oracle", "COLLABORATING", "Confirming 2BR price point with SOFIA", "model", ["sofia"]);
      e.msg("oracle", "sofia", "QUESTION", "Confirm price/m² for 2BR units", "Is the 2BR premium above the median?", "model", true);
      e.ev("oracle", "model", "consult", "sofia", "Confirm price/m² for 2BR units");
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
    s(1.2, (e) => {
      e.tupd("loi", undefined, 0.6);
      e.agent("alfred", "WORKING", "Writing LOI_Zapopan_draft.md", "loi");
      const d = e.deliverable("LOI_Zapopan_draft.md", LOI_TEXT);
      e.ev("alfred", "loi", "file_written", `atlas-local/outputs/corporate/${e.mission!.id}/${d.name}`, `${d.size_bytes} bytes`);
    }),
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
      e.ev("alfred", "loi", "approval", a.id, "EXTERNAL_COMMUNICATION · requested");
      e.agent("alfred", "BLOCKED", "Awaiting human approval to send LOI", "loi");
      e.agent("atlas", "WAITING", "Paused — human decision required");
      return "pause";
    }),
  ];
}

function scriptAfterDecision(decision: Decision): Step[] {
  const approved = decision === "APPROVED";
  const loi = (e: MockEngine) => e.deliverables.filter((d) => d.name.startsWith("LOI_"));
  const steps: Step[] = approved
    ? [
        s(0.6, (e) => {
          e.ev("alfred", "loi", "approval", e.lastApprovalId ?? "approval", "APPROVED by human");
          e.agent("alfred", "WORKING", "Sending LOI to seller's broker", "loi");
          e.tupd("loi", "IN_PROGRESS", 0.9);
        }),
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
            deliverables: loi(e),
          });
          e.msg("alfred", "atlas", "RESULT", "LOI sent", "Delivered to broker; follow-up scheduled in 10 business days.", "loi");
          e.agent("alfred", "COMPLETED", "LOI sent · follow-up scheduled");
        }),
      ]
    : [
        s(0.6, (e) => {
          e.ev("alfred", "loi", "approval", e.lastApprovalId ?? "approval", "REJECTED by human");
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
            deliverables: loi(e),
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
        version: m.round,
        deliverables: [...e.deliverables],
        created_at: now(),
      };
      e.publishReport(report);
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

/** Round N (mock follow-up): ATLAS answers, delegates one task to ORACLE, and issues report vN. */
function scriptFollowUp(question: string): Step[] {
  const short = question.length > 60 ? `${question.slice(0, 57)}…` : question;
  let ref = "";
  return [
    s(1.0, (e) => {
      e.tick();
      const m = e.mission!;
      const round = m.round + 1;
      ref = `followup-${round}`;
      e.thread(m.id, "atlas", "human", "ANSWER", `Starting round ${round}`, `Good question. I'm opening round ${round}: ORACLE will rerun the offer sensitivity with your note and I'll issue report v${round}.`);
      e.setMission({ ...m, round, phase: "DELEGATION", closed_at: null }, "mission.phase_changed", `Round ${round} started`);
      e.agent("atlas", "WORKING", `Round ${round}: delegating follow-up`);
      e.task(ref, { title: `Follow-up: ${short}`, description: question, assigned_to: "oracle", priority: "HIGH", depends_on: e.tasks.has("model") ? ["model"] : [] });
    }),
    s(0.8, (e) => {
      e.phase("EXECUTION");
      e.tupd(ref, "IN_PROGRESS", 0.2);
      e.agent("oracle", "WORKING", "Reading Comparables_2026.xlsx", ref);
      e.ev("oracle", ref, "file_read", "~/Documents/Corporate/Zapopan/Comparables_2026.xlsx", "sheet 'Comps' · 14 rows");
    }),
    s(1.0, (e) => {
      e.ev("oracle", ref, "consult", "sofia", "Closing-price discount on recent comps");
      e.tupd(ref, undefined, 0.6);
      e.agent("oracle", "WORKING", "Writing Offer_sensitivity.csv", ref);
    }),
    s(1.0, (e) => {
      const d = e.deliverable("Offer_sensitivity.csv", "offer_mxn_m,units,irr_pct\n52,120,24.1\n54,120,22.6\n56,120,21.0\n56,96,15.0\n58,120,19.4\n", "text/csv");
      e.ev("oracle", ref, "file_written", `atlas-local/outputs/corporate/${e.mission!.id}/${d.name}`, `${d.size_bytes} bytes`);
      e.tupd(ref, "COMPLETED", 1);
      e.report("oracle", ref, {
        asked_to: question,
        actions_taken: ["Re-ran offer sensitivity (52–58M × 96/120 units)", "Consulted SOFIA on closing-price discount"],
        inputs_used: ["Comparables_2026.xlsx", "SOFIA answer"],
        findings: [
          C("SCENARIO", "At MXN 54M the density-cut case still clears a 16.8% IRR.", "MEDIUM"),
          C("RECOMMENDATION", "Open at MXN 54M and keep 56M as the ceiling.", "MEDIUM"),
        ],
        unresolved: [],
        needs_agents: [],
        confidence: "MEDIUM",
        limitations: ["Closing-price discount estimated from 3 transactions"],
        deliverables: [d],
      });
      e.agent("oracle", "COMPLETED", "Follow-up delivered");
    }),
    s(0.8, (e) => { e.phase("CONSOLIDATION"); e.agent("atlas", "WORKING", "Consolidating round results"); }),
    s(1.0, (e) => {
      e.phase("REPORTING");
      const m = e.mission!;
      const prev = e.lastReport;
      const report: MissionReport = {
        ...(prev ?? ({} as MissionReport)),
        id: rid("rpt"),
        mission_id: m.id,
        executive_summary: `Round ${m.round}: ${prev?.executive_summary ?? ""} Follow-up: open at MXN 54M with 56M as the ceiling — the density-cut case still clears a 16.8% IRR at 54M.`,
        objective_status: prev?.objective_status ?? "ACHIEVED",
        key_findings: [C("RECOMMENDATION", "Open at MXN 54M; ceiling MXN 56M (ORACLE, round " + m.round + ").", "MEDIUM"), ...(prev?.key_findings ?? [])],
        tasks_completed: [...e.tasks.values()].filter((t) => t.status === "COMPLETED").map((t) => t.id),
        tasks_pending: [...e.tasks.values()].filter((t) => t.status !== "COMPLETED").map((t) => t.id),
        conflicts: prev?.conflicts ?? [],
        assumptions: prev?.assumptions ?? [],
        needs_human_attention: prev?.needs_human_attention ?? [],
        next_actions: prev?.next_actions ?? [],
        references: [],
        agent_report_ids: [],
        version: m.round,
        deliverables: [...e.deliverables],
        created_at: now(),
      };
      e.publishReport(report);
    }),
    s(1.0, (e) => {
      e.phase("CLOSED");
      e.agent("atlas", "COMPLETED", `Round ${e.mission!.round} closed · report v${e.mission!.round}`);
      e.agent("oracle", "IDLE", null);
    }),
  ];
}

const LOI_TEXT = `# Non-binding Letter of Intent — Zapopan lot (4,200 m²)

**Offer:** MXN 56,000,000
**Condition precedent:** final text of the land-use amendment does not reduce max density below 120 units.
**Due diligence:** 60 days from acceptance.

This letter is non-binding and is subject to a definitive purchase agreement.
`;

const ago = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();

/** Past missions (as if replayed from SQLite after a restart) for the history drawer. */
function seedHistory(): { missions: Mission[]; tasks: Task[] } {
  const base = {
    context: null,
    priority: "MEDIUM" as Priority,
    final_report_id: null,
    attachments: [] as Attachment[],
  };
  const interrupted: Mission = {
    ...base,
    id: "msn_hist_cashflow",
    objective: "Review the Q3 cash-flow forecast against budget and flag the three largest variances.",
    node: "corporate",
    mode: "live",
    usage: { input_tokens: 48200, output_tokens: 6100, cache_read_tokens: 21000, llm_calls: 9, est_cost_usd: 0.24 },
    phase: "EXECUTION",
    task_ids: ["tsk_hist_cf1", "tsk_hist_cf2"],
    round: 1,
    interrupted: true,
    attachments: [{ id: "att_hist_1", name: "Cashflow_Q3.xlsx", kind: "file", uri: null, content: null, size_bytes: 184_320, download_url: "/missions/msn_hist_cashflow/files/attachments/Cashflow_Q3.xlsx" }],
    created_at: ago(26),
    closed_at: null,
  };
  const closed: Mission = {
    ...base,
    id: "msn_hist_l10",
    objective: "Prepare the agenda and issues list for next Monday's Level 10 meeting.",
    node: "corporate",
    mode: "simulated",
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 },
    phase: "CLOSED",
    task_ids: [],
    round: 1,
    interrupted: false,
    created_at: ago(50),
    closed_at: ago(49.8),
  };
  const t = (id: string, title: string, agent: string): Task => ({
    id,
    mission_id: interrupted.id,
    title,
    description: title,
    assigned_to: agent,
    created_by: "atlas",
    status: "CANCELLED",
    priority: "MEDIUM",
    depends_on: [],
    requires_approval: false,
    approval_reason: null,
    progress: 0.4,
    parent_task_id: null,
    result_report_id: null,
    round: 1,
    created_at: ago(26),
    started_at: ago(25.9),
    completed_at: null,
  });
  return { missions: [interrupted, closed], tasks: [t("tsk_hist_cf1", "Variance analysis vs. budget", "oracle"), t("tsk_hist_cf2", "Pull actuals from Cashflow_Q3.xlsx", "sofia")] };
}

/* ---------------------------------------------------------------- transport */

function checkFiles(files: File[], existing: number) {
  if (existing + files.length > 20) throw new Error("ATLAS API 413: at most 20 files per mission");
  const big = files.find((f) => f.size > 25 * 1024 * 1024);
  if (big) throw new Error(`ATLAS API 413: ${big.name} is larger than 25 MB`);
}

function fileAttachment(missionId: string, f: File): Attachment {
  return {
    id: rid("att"),
    name: f.name,
    kind: "file",
    uri: null,
    content: null,
    size_bytes: f.size,
    // blob URL so the mock link really downloads what the user attached
    download_url: typeof URL !== "undefined" ? URL.createObjectURL(f) : `/missions/${missionId}/files/attachments/${encodeURIComponent(f.name)}`,
  };
}

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
    config: async () => mockConfig(),
    availability: async () => AVAILABILITY,
    inbox: engine.inbox.api,
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
      engine.setMission({ ...engine.mission!, phase: "CLOSED", closed_at: now() }, "mission.closed", "Mission cancelled by human");
      return engine.mission!;
    },
    async launch(body: LaunchMissionBody, files?: File[]) {
      if (body.mode === "live" && !mockConfig().live_available) throw new Error("ATLAS API 422: live mode requires ANTHROPIC_API_KEY");
      checkFiles(files ?? [], 0);
      engine.stop();
      engine.paused = false;
      engine.tasks.clear();
      engine.deliverables = [];
      engine.lastReport = null;
      const scenario = SCENARIOS.find((x) => x.id === body.scenario_id) ?? SCENARIOS[0];
      const id = rid("msn");
      const mission: Mission = {
        id,
        objective: body.objective?.trim() || scenario.objective,
        node: body.node,
        mode: body.mode === "live" ? "live" : "simulated",
        attachments: (files ?? []).map((f) => fileAttachment(id, f)),
        round: 1,
        interrupted: false,
        usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 },
        context: scenario.title,
        phase: "OBJECTIVE",
        priority: "HIGH",
        task_ids: [],
        final_report_id: null,
        created_at: now(),
        closed_at: null,
      };
      engine.setMission(mission, "mission.created", `Mission received: ${mission.objective}`);
      engine.agent("atlas", "WORKING", "Analyzing objective");
      engine.push(scriptOpening());
      return mission;
    },
    async decide(id: string, decision: Decision, note?: string) {
      const a = engine.pendingApproval;
      if (!a || a.id !== id) throw new Error("Approval not pending");
      const decided: ApprovalRequest = { ...a, state: decision, decision_note: note ?? null, decided_at: now() };
      engine.pendingApproval = null;
      engine.lastApprovalId = a.id;
      engine.emit("approval.decided", { approval: decided }, `Human ${decision.toLowerCase()}: ${a.title}`, null);
      engine.resume(scriptAfterDecision(decision));
      return decided;
    },
    async sendMessage(missionId: string, text: string) {
      const m = engine.missions.get(missionId);
      if (!m) throw new Error("ATLAS API 404: mission not found");
      const body = text.trim();
      if (!body) throw new Error("ATLAS API 422: empty message");
      const msg = engine.thread(m.id, "human", "atlas", "REQUEST", body.length > 60 ? `${body.slice(0, 57)}…` : body, body);
      const reply = (answer: string) =>
        setTimeout(() => engine.thread(m.id, "atlas", "human", "ANSWER", answer.slice(0, 60), answer), 900 / engine.speed);
      const current = engine.mission?.id === m.id ? engine.mission : m;
      if (current.mode !== "live") {
        reply("This is a simulated mission; follow-ups need a live mission.");
      } else if (current.phase !== "CLOSED" && engine.mission?.id === m.id && !current.interrupted) {
        reply("Noted — I'll add this to the mission guidance. Every task that starts from now on gets it, and I'll use it in review and consolidation.");
      } else if (engine.mission?.id === m.id && !engine.timer && !engine.paused) {
        engine.push(scriptFollowUp(body));
      } else {
        reply("The simulated stream can't resume this mission; launch a new one to try follow-up rounds.");
      }
      return msg;
    },
    async attach(missionId: string, files: File[]) {
      const m = engine.missions.get(missionId);
      if (!m) throw new Error("ATLAS API 404: mission not found");
      const current = engine.mission?.id === m.id ? engine.mission : m;
      checkFiles(files, current.attachments.length);
      const next: Mission = { ...current, attachments: [...current.attachments, ...files.map((f) => fileAttachment(m.id, f))] };
      if (engine.mission?.id === m.id) engine.setMission(next, "mission.updated", `${files.length} file${files.length === 1 ? "" : "s"} attached`);
      else {
        engine.missions.set(m.id, next);
        engine.emit("mission.updated", { mission: next }, `${files.length} file${files.length === 1 ? "" : "s"} attached`, "atlas");
      }
      return next;
    },
    async missions(node: string | null): Promise<MissionSummary[]> {
      return [...engine.missions.values()]
        .filter((m) => !node || m.node === node)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((m) => ({
          id: m.id,
          objective: m.objective,
          node: m.node,
          mode: m.mode,
          phase: m.phase,
          round: m.round,
          interrupted: m.interrupted,
          created_at: m.created_at,
          closed_at: m.closed_at,
          report_versions: engine.reportVersions.get(m.id) ?? 0,
          usage: m.usage,
        }));
    },
  };
}
