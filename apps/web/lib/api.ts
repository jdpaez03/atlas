import type { AgentDefinition, Alert, ApprovalRequest, AtlasEvent, Brief, EmailDraft, FollowUp, Mission, MissionPhase, RockStatus, Usage, WorldState } from "./contracts";

export const API_URL = (process.env.NEXT_PUBLIC_ATLAS_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/** Shape of GET /scenarios entries (docs/EVENTS.md). */
export interface Scenario {
  id: string;
  node: string;
  title: string;
  objective: string;
}

export interface LaunchMissionBody {
  objective: string;
  node: string;
  scenario_id?: string;
  speed?: number;
  mode?: MissionMode;
}

export type MissionMode = "simulated" | "live";

/** GET /missions?node= entries (docs/PHASE3.md §B), newest first. */
export interface MissionSummary {
  id: string;
  objective: string;
  node: string;
  mode: MissionMode;
  phase: MissionPhase;
  round: number;
  interrupted: boolean;
  created_at: string;
  closed_at: string | null;
  report_versions: number;
  usage: Usage | null;
}

/** Upload limits enforced by the API (docs/PHASE3.md §B). */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
export const MAX_FILES = 20;

/** Resolve an Attachment.download_url (an API path) to a clickable href. */
export const fileHref = (url: string | null | undefined): string | undefined =>
  !url ? undefined : /^(https?:|blob:|data:)/.test(url) ? url : `${API_URL}${url.startsWith("/") ? "" : "/"}${url}`;

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** GET /config (docs/LIVE.md). A 404 (Phase 1 backend) is treated as "live not available". */
export interface AtlasConfig {
  live_available: boolean;
  /** What runs live missions: the Claude API (key) or the user's Claude plan via Claude Code. */
  backend?: "api" | "subscription" | null;
  /** "api_equivalent": plan usage priced at API rates (not billed). */
  cost_basis?: "api" | "api_equivalent" | null;
  /** Why live is unavailable, in plain words. */
  live_hint?: string | null;
  models: { orchestrator?: string | null; default?: string | null };
  web_search: boolean;
  context_nodes: string[];
}

export const NO_LIVE_CONFIG: AtlasConfig = { live_available: false, models: {}, web_search: false, context_nodes: [] };

/** GET /agents/availability — `{agent_id: {available, reason?}}`. A 404 means "all available". */
export type Availability = Record<string, { available: boolean; reason?: string | null }>;

export type Decision = "APPROVED" | "REJECTED";

/** GET /usage?days=&node= — LLM usage over a window (Phase 5). */
export interface UsageReport {
  days: number;
  node: string | null;
  missions: number;
  totals: Usage;
  by_agent: Record<string, Usage>;
  by_day: { date: string; est_cost_usd: number; llm_calls: number }[];
  /** what runs live missions; on "subscription" est_cost_usd is an API-equivalent estimate, not a bill */
  backend: "subscription" | "api" | null;
  note: string;
}

export const ZERO_USAGE: Usage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`ATLAS API ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const wsUrl = (since: number) => `${API_URL.replace(/^http/, "ws")}/ws?since=${since}`;

export const api = {
  state: () => fetch(`${API_URL}/state`, { cache: "no-store" }).then((r) => json<WorldState>(r)),
  events: async (since: number): Promise<AtlasEvent[]> => {
    const body = await fetch(`${API_URL}/events?since=${since}`, { cache: "no-store" }).then((r) => json<unknown>(r));
    if (Array.isArray(body)) return body as AtlasEvent[];
    const wrapped = (body as { events?: AtlasEvent[] })?.events;
    return Array.isArray(wrapped) ? wrapped : [];
  },
  scenarios: () => fetch(`${API_URL}/scenarios`, { cache: "no-store" }).then((r) => json<Scenario[]>(r)),
  launch: (body: LaunchMissionBody, files?: File[]) => {
    if (!files?.length) {
      return fetch(`${API_URL}/missions`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }).then((r) => json<Mission>(r));
    }
    // multipart/form-data: objective, node, mode, scenario_id?, files (docs/PHASE3.md §B)
    const fd = new FormData();
    fd.append("objective", body.objective);
    fd.append("node", body.node);
    if (body.mode) fd.append("mode", body.mode);
    if (body.scenario_id) fd.append("scenario_id", body.scenario_id);
    if (body.speed != null) fd.append("speed", String(body.speed));
    for (const f of files) fd.append("files", f, f.name);
    return fetch(`${API_URL}/missions`, { method: "POST", body: fd }).then((r) => json<Mission>(r));
  },
  attach: (id: string, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    return fetch(`${API_URL}/missions/${encodeURIComponent(id)}/attachments`, { method: "POST", body: fd }).then((r) => json<unknown>(r));
  },
  sendMessage: (id: string, text: string) =>
    fetch(`${API_URL}/missions/${encodeURIComponent(id)}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    }).then((r) => json<unknown>(r)),
  /** null = the backend has no history endpoint (404). */
  missions: async (node?: string | null, limit = 100): Promise<MissionSummary[] | null> => {
    const q = new URLSearchParams();
    if (node) q.set("node", node);
    q.set("limit", String(limit));
    const res = await fetch(`${API_URL}/missions?${q}`, { cache: "no-store" });
    if (res.status === 404 || res.status === 405) return null;
    const body = await json<unknown>(res);
    const raw = Array.isArray(body) ? body : (body as { missions?: unknown[] })?.missions;
    if (!Array.isArray(raw)) return [];
    // The API sends report_versions as the list of versions ([1, 2]); the UI uses the latest number.
    return raw.map((m) => {
      const r = m as MissionSummary & { report_versions: number | number[] };
      const v = Array.isArray(r.report_versions) ? Math.max(0, ...r.report_versions) : r.report_versions ?? 0;
      return { ...r, report_versions: v };
    });
  },
  decide: (id: string, decision: Decision, note?: string) =>
    fetch(`${API_URL}/approvals/${encodeURIComponent(id)}/decision`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(note ? { decision, note } : { decision }),
    }).then((r) => json<ApprovalRequest>(r)),
  cancel: (id: string) =>
    fetch(`${API_URL}/missions/${encodeURIComponent(id)}/cancel`, { method: "POST" }).then((r) => json<Mission>(r)),
  /** POST /missions/{id}/resume — re-runs FAILED/CANCELLED tasks as a new round. 409 {detail} when refused. */
  resume: (id: string) => resumeMission(id),
  /** GET /usage — null when the backend has no usage endpoint (404). */
  usage: (days: number, node: string | null) => getUsage(days, node),
  config: async (): Promise<AtlasConfig> => {
    const res = await fetch(`${API_URL}/config`, { cache: "no-store" });
    if (res.status === 404) return NO_LIVE_CONFIG;
    const c = await json<Partial<AtlasConfig>>(res);
    return {
      live_available: !!c.live_available,
      backend: c.backend ?? null,
      cost_basis: c.cost_basis ?? null,
      live_hint: c.live_hint ?? null,
      models: c.models ?? {},
      web_search: !!c.web_search,
      context_nodes: Array.isArray(c.context_nodes) ? c.context_nodes : [],
    };
  },
  availability: async (): Promise<Availability> => {
    const res = await fetch(`${API_URL}/agents/availability`, { cache: "no-store" });
    if (res.status === 404) return {};
    return json<Availability>(res);
  },
};

export function resumeMission(id: string): Promise<Mission> {
  return fetch(`${API_URL}/missions/${encodeURIComponent(id)}/resume`, { method: "POST" }).then((r) => json<Mission>(r));
}

/** GET /usage?days=&node=. A 404/405 (backend without the endpoint) → null, like the other optional modules. */
export async function getUsage(days: number, node: string | null): Promise<UsageReport | null> {
  const q = new URLSearchParams({ days: String(days) });
  if (node) q.set("node", node);
  const res = await fetch(`${API_URL}/usage?${q}`, { cache: "no-store" });
  if (res.status === 404 || res.status === 405) return null;
  const b = await json<Partial<UsageReport>>(res);
  return {
    days: b.days ?? days,
    node: b.node ?? null,
    missions: b.missions ?? 0,
    totals: { ...ZERO_USAGE, ...(b.totals ?? {}) },
    by_agent: b.by_agent ?? {},
    by_day: Array.isArray(b.by_day) ? b.by_day : [],
    backend: b.backend ?? null,
    note: b.note ?? "",
  };
}

export async function getAgents(): Promise<AgentDefinition[]> {
  const res = await fetch(`${API_URL}/agents`, { cache: "no-store" });
  return json<AgentDefinition[]>(res);
}

/* ------------------------------------------------------------------------------------------------
 * Inbox (docs/INBOX.md §2). Every /inbox/* call maps a 404 to null so the UI can hide itself
 * when the backend has no inbox module.
 * ---------------------------------------------------------------------------------------------- */

export interface InboxStatus {
  /** "graph" | "folder"; null/"" = not configured */
  source: string | null;
  connected: boolean;
  account: string | null;
  last_scan: string | null;
  next_scan: string | null;
  schedule: string | string[] | null;
  processed_count: number;
  hint: string | null;
}

export interface InboxConnectStart {
  user_code: string;
  verification_uri: string;
  expires_in: number;
  message?: string | null;
}

/** Normalized GET /inbox/connect/status. */
export interface InboxConnectState {
  state: "pending" | "connected" | "error";
  account: string | null;
  error: string | null;
}

export type FollowUpPatch = Partial<Pick<FollowUp, "status" | "due" | "title" | "priority">>;

export interface DraftDecisionBody {
  decision: "APPROVED" | "DISCARDED";
  subject?: string;
  body?: string;
  to?: string[];
  cc?: string[];
}

/** The operations the Follow-ups view needs; implemented by the live API and by mock mode. */
export interface InboxApi {
  status(): Promise<InboxStatus | null>;
  scan(): Promise<unknown>;
  connect(): Promise<InboxConnectStart>;
  connectStatus(): Promise<InboxConnectState>;
  patchFollowup(id: string, patch: FollowUpPatch): Promise<FollowUp>;
  draftFollowup(id: string): Promise<unknown>;
  decideDraft(id: string, body: DraftDecisionBody): Promise<EmailDraft>;
  /** POST /digests/run {days} — a CC digest of the last N days; runs as a background mission. */
  runDigest(days: number): Promise<Mission>;
}

/** "ATLAS API 409: {\"detail\":\"…\"}" → "…" (plain words for the UI). */
export function apiErrorText(x: unknown, fallback = "Request failed"): string {
  const msg = x instanceof Error ? x.message : String(x ?? "");
  const m = /^ATLAS API (\d+)(?::\s*([\s\S]*))?$/.exec(msg);
  if (!m) return msg || fallback;
  const body = m[2] ?? "";
  try {
    const d = (JSON.parse(body) as { detail?: unknown }).detail;
    if (typeof d === "string" && d) return d;
    if (Array.isArray(d)) return d.map((e) => (e as { msg?: string }).msg ?? "").filter(Boolean).join("; ") || fallback;
  } catch {
    /* not JSON */
  }
  return body || `${fallback} (${m[1]})`;
}

export const DRAFT_EML_URL = (id: string) => `${API_URL}/drafts/${encodeURIComponent(id)}/eml`;
export const INBOX_SETUP_DOC = "https://github.com/jdpaez03/atlas/blob/main/docs/INBOX_SETUP.md";

export function normalizeConnectState(body: unknown): InboxConnectState {
  const b = (body ?? {}) as Record<string, unknown>;
  const raw = String(b.state ?? b.status ?? "").toLowerCase();
  const err = (b.hint ?? b.error ?? b.detail ?? null) as string | null; // hint is the plain-language one
  const connected = b.connected === true || raw === "connected" || raw === "ok" || raw === "done";
  const failed = !connected && (raw === "error" || raw === "expired" || raw === "failed" || raw === "declined" || (!!b.error && raw !== "pending"));
  return {
    state: connected ? "connected" : failed ? "error" : "pending",
    account: (b.account as string | null) ?? null,
    error: failed ? (err ?? (raw === "expired" ? "The code expired — start again." : "Sign-in failed.")) : null,
  };
}

const post = (path: string, body?: unknown) =>
  fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const inboxApi: InboxApi = {
  status: async () => {
    const res = await fetch(`${API_URL}/inbox/status`, { cache: "no-store" });
    if (res.status === 404) return null;
    return json<InboxStatus>(res);
  },
  scan: () => post("/inbox/scan").then((r) => json<unknown>(r)),
  connect: () => post("/inbox/connect").then((r) => json<InboxConnectStart>(r)),
  connectStatus: () => fetch(`${API_URL}/inbox/connect/status`, { cache: "no-store" }).then((r) => json<unknown>(r)).then(normalizeConnectState),
  patchFollowup: (id, patch) =>
    fetch(`${API_URL}/followups/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then((r) => json<FollowUp>(r)),
  draftFollowup: (id) => post(`/followups/${encodeURIComponent(id)}/draft`).then((r) => json<unknown>(r)),
  decideDraft: (id, body) => post(`/drafts/${encodeURIComponent(id)}/decision`, body).then((r) => json<EmailDraft>(r)),
  runDigest: (days) => post("/digests/run", { days }).then((r) => json<Mission>(r)),
};

/* ------------------------------------------------------------------------------------------------
 * ARGOS monitoring (docs/ARGOS.md § Framework). GET /argos/status maps a 404 to null so the Monitor
 * view can say the backend has no ARGOS module.
 * ---------------------------------------------------------------------------------------------- */

export interface ArgosCheckStatus {
  /** "dashboards" | "l10" | "rocks" */
  name: string;
  /** false = not configured; `note` carries the hint */
  enabled: boolean;
  last_run: string | null;
  last_ok: boolean | null;
  note: string | null;
  /** backend state: ok | partial | failed | not configured | never run | not installed */
  state?: string | null;
  /** what to set up, when not configured */
  hint?: string | null;
}

export interface ArgosStatus {
  checks: ArgosCheckStatus[];
  next_run: string | null;
  next_brief: string | null;
  running: boolean;
  mission_id: string | null;
}

export type AlertStatus = Alert["status"];

export interface ArgosApi {
  status(): Promise<ArgosStatus | null>;
  run(checks?: string[]): Promise<Mission>;
  patchAlert(id: string, status: AlertStatus): Promise<Alert>;
  reloadRocks(): Promise<unknown>;
  patchRock(id: string, current: number): Promise<RockStatus>;
  buildBrief(): Promise<Mission>;
  briefs(): Promise<Brief[]>;
  /** where "Download .docx" points */
  briefFileUrl(brief: Brief): string | undefined;
  /** PAGA Suite sign-in (Microsoft Entra device code): POST /argos/suite/connect */
  suiteConnect(): Promise<InboxConnectStart>;
  /** GET /argos/suite/connect/status → {state: idle|pending|connected|failed, account?, error?, hint?} */
  suiteConnectStatus(): Promise<InboxConnectState>;
}

function normalizeArgosStatus(body: unknown): ArgosStatus {
  const b = (body ?? {}) as Partial<ArgosStatus> & Record<string, unknown>;
  const checks = Array.isArray(b.checks) ? b.checks : [];
  return {
    checks: checks.map((c) => {
      const x = (c ?? {}) as Partial<ArgosCheckStatus> & Record<string, unknown>;
      return {
        name: String(x.name ?? "check"),
        enabled: x.enabled !== false,
        last_run: (x.last_run as string | null) ?? null,
        last_ok: typeof x.last_ok === "boolean" ? x.last_ok : null,
        note: ((x.note ?? x.hint) as string | null) ?? null,
        state: typeof x.state === "string" ? x.state : null,
        hint: typeof x.hint === "string" ? x.hint : null,
      };
    }),
    next_run: b.next_run ?? null,
    next_brief: b.next_brief ?? null,
    running: !!b.running,
    mission_id: b.mission_id ?? null,
  };
}

const patchJson = (path: string, body: unknown) =>
  fetch(`${API_URL}${path}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

export const argosApi: ArgosApi = {
  status: async () => {
    const res = await fetch(`${API_URL}/argos/status`, { cache: "no-store" });
    if (res.status === 404) return null;
    return normalizeArgosStatus(await json<unknown>(res));
  },
  run: (checks) => post("/argos/run", checks?.length ? { checks } : {}).then((r) => json<Mission>(r)),
  patchAlert: (id, status) => patchJson(`/alerts/${encodeURIComponent(id)}`, { status }).then((r) => json<Alert>(r)),
  reloadRocks: () => post("/rocks/reload").then((r) => json<unknown>(r)),
  patchRock: (id, current) => patchJson(`/rocks/${encodeURIComponent(id)}`, { current }).then((r) => json<RockStatus>(r)),
  buildBrief: () => post("/argos/brief").then((r) => json<Mission>(r)),
  briefs: async () => {
    const body = await fetch(`${API_URL}/briefs`, { cache: "no-store" }).then((r) => json<unknown>(r));
    const list = Array.isArray(body) ? body : (body as { briefs?: unknown[] })?.briefs;
    return Array.isArray(list) ? (list as Brief[]) : [];
  },
  briefFileUrl: (b) => `${API_URL}/briefs/${encodeURIComponent(b.id)}/file`,
  suiteConnect: () => post("/argos/suite/connect").then((r) => json<InboxConnectStart>(r)),
  suiteConnectStatus: () =>
    fetch(`${API_URL}/argos/suite/connect/status`, { cache: "no-store" }).then((r) => json<unknown>(r)).then(normalizeConnectState),
};
