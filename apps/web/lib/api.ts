import type { AgentDefinition, ApprovalRequest, AtlasEvent, Mission, WorldState } from "./contracts";

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

/** GET /config (docs/LIVE.md). A 404 (Phase 1 backend) is treated as "live not available". */
export interface AtlasConfig {
  live_available: boolean;
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
  launch: (body: LaunchMissionBody) =>
    fetch(`${API_URL}/missions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<Mission>(r)),
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
