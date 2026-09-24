import type { AgentDefinition } from "./contracts";

export const API_URL = process.env.NEXT_PUBLIC_ATLAS_API_URL ?? "http://localhost:8000";

export async function getAgents(): Promise<AgentDefinition[]> {
  const res = await fetch(`${API_URL}/agents`, { cache: "no-store" });
  if (!res.ok) throw new Error(`ATLAS API responded ${res.status}`);
  return res.json();
}
