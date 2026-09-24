"use client";

import { useEffect } from "react";
import type { MissionSummary } from "@/lib/api";
import { cx, human } from "@/lib/ui";
import { ModeBadge } from "./MissionPanel";
import { Tag } from "./primitives";

const fmtWhen = (iso: string) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  return sameDay ? `Today ${time}` : `${d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" })} ${time}`;
};

const fmtUsd = (n: number) => (n > 0 && n < 0.01 ? "<$0.01" : `$${n.toFixed(2)}`);

/**
 * Mission history drawer (docs/PHASE3.md §B): GET /missions?node=, newest first.
 * Selecting a row shows that mission — the world state already holds every mission.
 */
export function MissionHistory({
  open,
  onClose,
  items,
  loading,
  error,
  currentId,
  known,
  onSelect,
  onRefresh,
  nodeName,
}: {
  open: boolean;
  onClose: () => void;
  items: MissionSummary[];
  loading: boolean;
  error: string | null;
  currentId: string | null;
  /** mission ids present in the world state (selectable) */
  known: Set<string>;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  nodeName: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <div className={cx("fixed inset-0 z-40", open ? "pointer-events-auto" : "pointer-events-none")} aria-hidden={!open}>
      <div className={cx("absolute inset-0 bg-black/55 backdrop-blur-[2px] transition-opacity", open ? "opacity-100" : "opacity-0")} onClick={onClose} />
      <aside
        role="dialog"
        aria-label="Mission history"
        className={cx(
          "absolute top-0 right-0 flex h-full w-full max-w-[440px] flex-col border-l border-edge bg-panel shadow-[-20px_0_60px_-20px_rgba(0,0,0,0.9)] transition-transform duration-200",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        <header className="flex items-center justify-between gap-3 border-b border-edge/80 px-4 py-3">
          <div>
            <h2 className="label flex items-center gap-2 !text-slate-300">
              <span className="text-signal/60">09</span>
              <span className="text-signal/30">//</span>
              Mission history
            </h2>
            <p className="mt-1 font-mono text-[9.5px] tracking-[0.08em] text-mute">
              {nodeName} node · {items.length} mission{items.length === 1 ? "" : "s"} · newest first
            </p>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={onRefresh} className="rounded px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-dim hover:bg-white/[0.04] hover:text-slate-200" disabled={loading}>
              {loading ? "Loading" : "Refresh"}
            </button>
            <button onClick={onClose} aria-label="Close history" className="flex h-7 w-7 items-center justify-center rounded text-[16px] text-dim hover:bg-white/[0.04] hover:text-slate-200">
              ×
            </button>
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
          {error && <p className="px-4 py-3 font-mono text-[10.5px] text-red-400">{error}</p>}
          {!error && items.length === 0 && !loading && <p className="px-4 py-8 text-center font-mono text-[11px] text-mute">No missions on this node yet.</p>}
          <ol className="divide-y divide-edge/60">
            {items.map((m) => {
              const current = m.id === currentId;
              const selectable = known.has(m.id);
              const closed = m.phase === "CLOSED";
              return (
                <li key={m.id}>
                  <button
                    disabled={!selectable}
                    onClick={() => {
                      onSelect(m.id);
                      onClose();
                    }}
                    className={cx(
                      "relative flex w-full flex-col gap-1.5 px-4 py-3 text-left transition",
                      current ? "bg-signal/[0.06]" : "hover:bg-white/[0.03]",
                      !selectable && "cursor-not-allowed opacity-50",
                    )}
                    title={selectable ? undefined : "Not in the loaded state"}
                  >
                    {current && <span className="absolute top-2 bottom-2 left-0 w-[2px] bg-signal" />}
                    <span className="flex flex-wrap items-center gap-1.5">
                      <ModeBadge mode={m.mode} closed={closed || m.interrupted} />
                      {m.interrupted && !closed ? <Tag color="#f87171">Interrupted</Tag> : closed ? <Tag color="#cbd5e1">Closed</Tag> : <Tag color="#22c55e">{human(m.phase)}</Tag>}
                      {m.round > 1 && <Tag color="#c4b5fd">Round {m.round}</Tag>}
                      {m.report_versions > 0 && (
                        <Tag color="#34d399">
                          Report{m.report_versions > 1 ? ` v${m.report_versions}` : ""}
                        </Tag>
                      )}
                      {current && <span className="ml-auto font-mono text-[9px] uppercase tracking-[0.18em] text-signal">Viewing</span>}
                    </span>
                    <span className="line-clamp-2 text-[12.5px] leading-snug text-slate-200">{m.objective}</span>
                    <span className="flex flex-wrap items-center gap-x-3 font-mono text-[9.5px] text-mute">
                      <span>{fmtWhen(m.created_at)}</span>
                      {m.closed_at && <span>closed {fmtWhen(m.closed_at)}</span>}
                      {m.mode === "live" && m.usage && (
                        <span>
                          {m.usage.llm_calls} calls · <span className="text-emerald-300/80">{fmtUsd(m.usage.est_cost_usd)}</span>
                        </span>
                      )}
                      <span className="ml-auto">{m.id}</span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      </aside>
    </div>
  );
}
