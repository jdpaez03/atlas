import type { AgentDefinition, ApprovalRequest, AtlasEvent, Mission, MissionPhase, Usage, WorldState } from "./contracts";

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

export async function getAgents(): Promise<AgentDefinition[]> {
  const res = await fetch(`${API_URL}/agents`, { cache: "no-store" });
  return json<AgentDefinition[]>(res);
}
