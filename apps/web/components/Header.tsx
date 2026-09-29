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

export type View = "ask" | "missions" | "followups" | "monitor" | "usage" | "lessons";

const TABS: { id: View; label: string; short: string; key: string }[] = [
  { id: "ask", label: "Ask ATLAS", short: "Ask", key: "0" },
  { id: "missions", label: "Missions", short: "Missions", key: "1" },
  { id: "followups", label: "Follow-ups", short: "Follow-ups", key: "2" },
  { id: "monitor", label: "Monitor", short: "Monitor", key: "3" },
  { id: "usage", label: "Usage", short: "Usage", key: "4" },
  { id: "lessons", label: "Lessons", short: "Lessons", key: "5" },
];

const TAB_ICON: Record<View, ReactNode> = {
  ask: <path d="M4 5h16v10H9l-5 4V5z" />,
  missions: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /></>,
  followups: <><path d="M4 7l8 6 8-6" /><rect x="4" y="5" width="16" height="14" rx="2" /></>,
  monitor: <path d="M3 12h4l2-6 4 12 2-6h6" />,
  usage: <path d="M5 19V11M12 19V5M19 19v-6" />,
  lessons: <path d="M5 4h10l4 4v12H5zM9 12h6M9 16h6" />,
};

function badgeFor(id: View, counts: { open: number; overdue: number }, highAlerts: number, proposedLessons: number) {
  if (id === "followups" && counts.overdue > 0) return { n: counts.overdue, cls: "bg-red-500 text-white" };
  if (id === "followups" && counts.open > 0) return { n: counts.open, cls: "bg-slate-600 text-white" };
  if (id === "monitor" && highAlerts > 0) return { n: highAlerts, cls: "bg-red-500 text-white" };
  if (id === "lessons" && proposedLessons > 0) return { n: proposedLessons, cls: "bg-amber-400 text-black" };
  return null;
}

/** Phones: the views as a fixed bottom tab bar (the top header keeps only the wordmark, node and status). */
export function MobileNav({ view, onView, counts, highAlerts = 0, proposedLessons = 0 }: { view: View; onView: (v: View) => void; counts: { open: number; overdue: number }; highAlerts?: number; proposedLessons?: number }) {
  return (
    <nav
      aria-label="View"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-edge/80 bg-void/95 backdrop-blur-md lg:hidden"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <div className="grid grid-cols-6">
        {TABS.map((t) => {
          const active = view === t.id;
          const b = badgeFor(t.id, counts, highAlerts, proposedLessons);
          return (
            <button
              key={t.id}
              onClick={() => onView(t.id)}
              aria-current={active ? "page" : undefined}
              className={cx("relative flex h-14 flex-col items-center justify-center gap-1 font-mono text-[9px] uppercase tracking-[0.08em]", active ? "text-signal" : "text-dim")}
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                {TAB_ICON[t.id]}
              </svg>
              <span className="max-w-full truncate px-0.5">{t.short}</span>
              {b && (
                <span className={cx("absolute right-[18%] top-1.5 min-w-[16px] rounded-full px-1 text-center text-[9px] leading-[16px] tracking-normal tabular-nums", b.cls)}>{b.n}</span>
              )}
              {active && <span className="absolute inset-x-4 top-0 h-0.5 rounded-full bg-signal" />}
            </button>
          );
        })}
      </div>
    </nav>
  );
}

function ViewSwitch({ view, onView, counts, highAlerts, proposedLessons = 0 }: { view: View; onView: (v: View) => void; counts: { open: number; overdue: number }; highAlerts: number; proposedLessons?: number }) {
  return (
    <div role="tablist" aria-label="View" className="hidden shrink-0 items-center rounded-md border border-edge bg-black/30 p-0.5 lg:flex">
      {TABS.map((t) => {
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
            {t.id === "lessons" && proposedLessons > 0 && (
              <span className="rounded-full bg-amber-400/20 px-1.5 text-[9.5px] leading-[16px] tracking-normal text-amber-200 tabular-nums" title={`${proposedLessons} lesson${proposedLessons === 1 ? "" : "s"} waiting for approval`}>
                {proposedLessons}
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
  proposedLessons = 0,
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
  proposedLessons?: number;
  /** the inbox status chip (renders nothing when the backend has no inbox) */
  inbox?: ReactNode;
}) {
  const now = useNow(1000);
  const c = CONN[conn];
  const d = now ? new Date(now) : null;

  return (
    <header className="sticky top-0 z-30 border-b border-edge/80 bg-void/80 backdrop-blur-md">
      <div className="mx-auto flex h-12 max-w-[1680px] items-center gap-3 px-3 md:h-14 md:gap-4 md:px-4 lg:gap-5 lg:px-6">
        {/* wordmark */}
        <div className="flex shrink-0 items-center gap-3">
          <Emblem size={26} />
          <div className="leading-none">
            <div className="font-mono text-[16px] font-semibold tracking-[0.34em] text-ink md:text-[19px] md:tracking-[0.42em]">ATLAS</div>
            <div className="mt-1 hidden font-mono text-[9px] tracking-[0.26em] text-dim uppercase 2xl:block">
              The intelligence behind the intelligence
            </div>
          </div>
        </div>

        <ViewSwitch view={view} onView={onView} counts={counts} highAlerts={highAlerts} proposedLessons={proposedLessons} />

        {/* node switcher */}
        <nav className="flex min-w-0 flex-1 items-center justify-end gap-1 lg:justify-center" aria-label="Nodes">
          {nodes.map((n) => {
            const active = n.id === node;
            return (
              <button
                key={n.id}
                disabled={!n.enabled}
                onClick={() => {
                  if (!n.enabled) return;
                  // phones show only the active node: tapping it moves to the next enabled one
                  const enabled = nodes.filter((x) => x.enabled);
                  if (active && enabled.length > 1 && window.matchMedia("(max-width: 1023px)").matches) {
                    onNode(enabled[(enabled.findIndex((x) => x.id === n.id) + 1) % enabled.length].id);
                  } else onNode(n.id);
                }}
                title={n.enabled ? n.description : `${n.name} — coming soon`}
                className={cx(
                  "group relative items-center gap-2 rounded-md px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.16em] transition md:px-3.5 md:text-[11px] md:tracking-[0.2em]",
                  active ? "flex text-ink" : n.enabled ? "hidden text-dim hover:bg-white/[0.03] hover:text-slate-200 lg:flex" : "hidden cursor-not-allowed text-mute/70 lg:flex",
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
                {active && <span className="absolute inset-x-3 -bottom-[11px] hidden h-px lg:block" style={{ background: n.color, boxShadow: `0 0 10px ${n.color}` }} />}
              </button>
            );
          })}
        </nav>

        {/* connection + clock */}
        <div className="flex shrink-0 items-center gap-3">
          <div className="hidden md:block lg:hidden xl:block">{inbox}</div>
          <div
            className="flex items-center gap-2 rounded-full border px-2 py-1 font-mono text-[10px] uppercase tracking-[0.2em] md:px-2.5"
            style={{ color: c.color, borderColor: `${c.color}44`, background: `${c.color}10` }}
            title={`Connection: ${c.label}`}
          >
            <span className={cx("h-1.5 w-1.5 rounded-full", c.pulse && "dot-live")} style={{ background: c.color, ["--c" as string]: `${c.color}aa` }} />
            <span className="hidden sm:inline lg:hidden xl:inline">{c.label}</span>
          </div>
          <div className="hidden text-right font-mono leading-none xl:block">
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
