"use client";

import type { ReactNode } from "react";
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

export type View = "missions" | "followups" | "monitor" | "usage";

function ViewSwitch({ view, onView, counts, highAlerts }: { view: View; onView: (v: View) => void; counts: { open: number; overdue: number }; highAlerts: number }) {
  const tabs: { id: View; label: string; key: string }[] = [
    { id: "missions", label: "Missions", key: "1" },
    { id: "followups", label: "Follow-ups", key: "2" },
    { id: "monitor", label: "Monitor", key: "3" },
    { id: "usage", label: "Usage", key: "4" },
  ];
  return (
    <div role="tablist" aria-label="View" className="flex shrink-0 items-center rounded-md border border-edge bg-black/30 p-0.5">
      {tabs.map((t) => {
        const active = view === t.id;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={active}
            onClick={() => onView(t.id)}
            title={`${t.label} (${t.key})`}
            className={cx(
              "flex h-7 items-center gap-1.5 rounded px-2.5 font-mono text-[10.5px] uppercase tracking-[0.16em] transition",
              active ? "bg-signal/[0.12] text-ink shadow-[inset_0_0_0_1px_rgba(125,211,252,0.3)]" : "text-dim hover:text-slate-200",
            )}
          >
            {t.label}
            {t.id === "followups" && counts.open > 0 && (
              <span className="flex items-center gap-1 tracking-normal">
                <span className={cx("rounded-full px-1.5 text-[9.5px] leading-[16px] tabular-nums", active ? "bg-white/10 text-slate-200" : "bg-white/[0.06] text-slate-300")}>
                  {counts.open}
                </span>
                {counts.overdue > 0 && (
                  <span className="rounded-full bg-red-500/20 px-1.5 text-[9.5px] leading-[16px] text-red-300 tabular-nums" title={`${counts.overdue} overdue`}>
                    {counts.overdue}
                  </span>
                )}
              </span>
            )}
            {t.id === "monitor" && highAlerts > 0 && (
              <span className="rounded-full bg-red-500/25 px-1.5 text-[9.5px] leading-[16px] tracking-normal text-red-200 tabular-nums" title={`${highAlerts} open HIGH alert${highAlerts === 1 ? "" : "s"}`}>
                {highAlerts}
              </span>
            )}
            <kbd className="hidden rounded border border-edge px-1 text-[8.5px] leading-[13px] text-mute 2xl:inline">{t.key}</kbd>
          </button>
        );
      })}
    </div>
  );
}

export function Header({
  nodes,
  node,
  onNode,
  conn,
  view,
  onView,
  counts,
  highAlerts = 0,
  inbox,
}: {
  nodes: NodeDefinition[];
  node: string | null;
  onNode: (id: string) => void;
  conn: ConnStatus;
  view: View;
  onView: (v: View) => void;
  counts: { open: number; overdue: number };
  /** open HIGH alerts → red count on the Monitor tab */
  highAlerts?: number;
  /** the inbox status chip (renders nothing when the backend has no inbox) */
  inbox?: ReactNode;
}) {
  const now = useNow(1000);
  const c = CONN[conn];
  const d = now ? new Date(now) : null;

  return (
    <header className="sticky top-0 z-30 border-b border-edge/80 bg-void/80 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-[1680px] items-center gap-4 px-4 lg:gap-5 lg:px-6">
        {/* wordmark */}
        <div className="flex shrink-0 items-center gap-3">
          <Emblem size={30} />
          <div className="leading-none">
            <div className="font-mono text-[19px] font-semibold tracking-[0.42em] text-ink">ATLAS</div>
            <div className="mt-1 hidden font-mono text-[9px] tracking-[0.26em] text-dim uppercase 2xl:block">
              The intelligence behind the intelligence
            </div>
          </div>
        </div>

        <ViewSwitch view={view} onView={onView} counts={counts} highAlerts={highAlerts} />

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
        <div className="flex shrink-0 items-center gap-3">
          {inbox}
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
