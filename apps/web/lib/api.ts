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
}

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
};

export async function getAgents(): Promise<AgentDefinition[]> {
  const res = await fetch(`${API_URL}/agents`, { cache: "no-store" });
  return json<AgentDefinition[]>(res);
}
