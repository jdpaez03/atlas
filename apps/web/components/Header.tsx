"use client";

import type { NodeDefinition } from "@/lib/contracts";
import type { ConnStatus } from "@/lib/store";
import { cx, useNow } from "@/lib/ui";
import { Emblem } from "./primitives";

const CONN: Record<ConnStatus, { label: string; color: string; pulse: boolean }> = {
  connecting: { label: "Connecting", color: "#eab308", pulse: true },
  live: { label: "Live", color: "#22c55e", pulse: true },
  reconnecting: { label: "Reconnecting", color: "#f59e0b", pulse: true },
  offline: { label: "Offline", color: "#ef4444", pulse: false },
  mock: { label: "Simulated", color: "#a78bfa", pulse: true },
};

export function Header({
  nodes,
  node,
  onNode,
  conn,
}: {
  nodes: NodeDefinition[];
  node: string | null;
  onNode: (id: string) => void;
  conn: ConnStatus;
}) {
  const now = useNow(1000);
  const c = CONN[conn];
  const d = now ? new Date(now) : null;

  return (
    <header className="sticky top-0 z-30 border-b border-edge/80 bg-void/80 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-[1680px] items-center gap-6 px-4 lg:px-6">
        {/* wordmark */}
        <div className="flex shrink-0 items-center gap-3">
          <Emblem size={30} />
          <div className="leading-none">
            <div className="font-mono text-[19px] font-semibold tracking-[0.42em] text-ink">ATLAS</div>
            <div className="mt-1 hidden font-mono text-[9px] tracking-[0.26em] text-dim uppercase xl:block">
              The intelligence behind the intelligence
            </div>
          </div>
        </div>

        {/* node switcher */}
        <nav className="flex min-w-0 flex-1 items-center justify-center gap-1" aria-label="Nodes">
          {nodes.map((n) => {
            const active = n.id === node;
            return (
              <button
                key={n.id}
                disabled={!n.enabled}
                onClick={() => n.enabled && onNode(n.id)}
                title={n.enabled ? n.description : `${n.name} — coming soon`}
                className={cx(
                  "group relative flex items-center gap-2 rounded-md px-3.5 py-1.5 font-mono text-[11px] uppercase tracking-[0.2em] transition",
                  active ? "text-ink" : n.enabled ? "text-dim hover:bg-white/[0.03] hover:text-slate-200" : "cursor-not-allowed text-mute/70",
                )}
                style={active ? { background: `${n.color}12`, boxShadow: `inset 0 0 0 1px ${n.color}40` } : undefined}
              >
                {n.enabled ? (
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: n.color, boxShadow: active ? `0 0 8px ${n.color}` : undefined, opacity: active ? 1 : 0.55 }} />
                ) : (
                  <svg width="10" height="10" viewBox="0 0 16 16" fill="none" aria-hidden>
                    <rect x="3" y="7" width="10" height="7" rx="1.5" stroke="currentColor" />
                    <path d="M5.5 7V5a2.5 2.5 0 015 0v2" stroke="currentColor" />
                  </svg>
                )}
                {n.name}
                {!n.enabled && <span className="rounded border border-edge px-1 text-[8px] tracking-[0.16em] text-mute">soon</span>}
                {active && <span className="absolute inset-x-3 -bottom-[11px] h-px" style={{ background: n.color, boxShadow: `0 0 10px ${n.color}` }} />}
              </button>
            );
          })}
        </nav>

        {/* connection + clock */}
        <div className="flex shrink-0 items-center gap-4">
          <div
            className="flex items-center gap-2 rounded-full border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.2em]"
            style={{ color: c.color, borderColor: `${c.color}44`, background: `${c.color}10` }}
            title={`Connection: ${c.label}`}
          >
            <span className={cx("h-1.5 w-1.5 rounded-full", c.pulse && "dot-live")} style={{ background: c.color, ["--c" as string]: `${c.color}aa` }} />
            {c.label}
          </div>
          <div className="hidden text-right font-mono leading-none lg:block">
            <div className="text-[15px] tabular-nums tracking-[0.12em] text-ink">{d ? d.toLocaleTimeString("en-GB", { hour12: false }) : "--:--:--"}</div>
            <div className="mt-1 text-[9px] uppercase tracking-[0.2em] text-dim">
              {d ? d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" }) : " "}
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}
