import { getAgents } from "@/lib/api";
import type { AgentDefinition } from "@/lib/contracts";

export const dynamic = "force-dynamic";

export default async function CommandCenter() {
  let agents: AgentDefinition[] = [];
  let error: string | null = null;
  try {
    agents = await getAgents();
  } catch (e) {
    error = e instanceof Error ? e.message : "API unreachable";
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-10 font-mono">
      <header className="mb-10 flex items-end justify-between border-b border-edge pb-4">
        <div>
          <p className="text-xs tracking-[0.4em] text-signal/70">MULTI-AGENT COMMAND CENTER</p>
          <h1 className="text-4xl font-semibold tracking-[0.3em] text-slate-100">ATLAS</h1>
        </div>
        <p className="text-xs text-slate-500">One Intelligence. Many Agents. · Phase 0</p>
      </header>

      <section>
        <h2 className="mb-4 text-xs tracking-[0.3em] text-slate-500">AGENT BOARD</h2>
        {error ? (
          <p className="rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-300">
            ATLAS API offline ({error}). Start it with <code>uv run uvicorn atlas.main:app</code> in apps/api.
          </p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {agents.map((a) => (
              <article key={a.id} className="rounded-lg border border-edge bg-panel/80 p-5"
                style={{ boxShadow: `inset 0 1px 0 ${a.color}33` }}>
                <div className="flex items-center justify-between">
                  <h3 className="text-lg tracking-[0.2em]" style={{ color: a.color }}>{a.name}</h3>
                  <span className="flex items-center gap-2 text-[10px] tracking-widest text-slate-400">
                    <span className="h-2 w-2 rounded-full bg-slate-400" /> IDLE
                  </span>
                </div>
                <p className="mt-1 text-xs uppercase tracking-widest text-slate-500">{a.title}</p>
                <p className="mt-3 text-sm leading-relaxed text-slate-300">{a.description}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
