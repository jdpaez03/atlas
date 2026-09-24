"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { apiErrorText, type ArgosApi, type ArgosCheckStatus, type ArgosStatus } from "@/lib/api";
import type { Alert, AlertEvidence, Brief, Mission, RockStatus } from "@/lib/contracts";
import { dayDiff, parseDue } from "@/lib/followups";
import { cx, useNow } from "@/lib/ui";
import { createPortal } from "react-dom";
import { DeviceCodeModal, type DeviceCodeCopy } from "./DeviceCodeModal";
import { Empty, Panel, Tag } from "./primitives";

const SUITE_CONNECT_COPY: DeviceCodeCopy = {
  eyebrow: "Connect PAGA Suite",
  title: "Sign in to PAGA Suite with Microsoft",
  account: "the work account you use for PAGA Suite",
  connected: "ARGOS will read the L10 now.",
  footer: "ATLAS signs in as you with read access to the L10 (to-dos, issues, weekly summaries). It never writes to the Suite.",
};

/* ------------------------------------------------------------------------------------------------ vocabulary */

const SEVERITY: Record<Alert["severity"], { color: string; label: string; rank: number }> = {
  HIGH: { color: "#f87171", label: "HIGH", rank: 0 },
  MEDIUM: { color: "#fbbf24", label: "MED", rank: 1 },
  LOW: { color: "#94a3b8", label: "LOW", rank: 2 },
};

const KIND: Record<Alert["kind"], string> = {
  missing_report: "Missing report",
  identical_report: "Identical report",
  moved_date: "Moved date",
  removed_row: "Removed row",
  kpi_mismatch: "KPI mismatch",
  value_change: "Value change",
  overdue_todo: "Overdue to-do",
  unreported_todo: "Unreported to-do",
  stale_issue: "Stale issue",
  rock_failed: "Rock failed",
  rock_at_risk: "Rock at risk",
  other: "Other",
};

const CHECK_LABEL: Record<string, string> = { dashboards: "Dashboards", l10: "L10", rocks: "Rocks" };
const checkLabel = (c: string) => CHECK_LABEL[c] ?? c.charAt(0).toUpperCase() + c.slice(1);
const CHECK_ORDER = ["dashboards", "l10", "rocks"];

export const ROCK_STATUS: Record<RockStatus["status"], { color: string; label: string }> = {
  ON_TRACK: { color: "#22c55e", label: "On track" },
  AT_RISK: { color: "#f59e0b", label: "At risk" },
  OFF_TRACK: { color: "#ef4444", label: "Off track" },
  FAILED: { color: "#ef4444", label: "Failed" },
  UNKNOWN: { color: "#64748b", label: "Unknown" },
  DONE: { color: "#e2e8f0", label: "Done" },
};

const PROJECT_COLORS = ["#7dd3fc", "#f0abfc", "#34d399", "#fb923c", "#a78bfa", "#2dd4bf", "#fde68a"];
type ColorOf = (project: string) => string;
/** Distinct colours by sorted project name, so a handful of projects never share one. */
function projectPalette(names: (string | null | undefined)[]): ColorOf {
  const list = [...new Set(names.filter((x): x is string => !!x?.trim()).map((x) => x.trim()))].sort((a, b) => a.localeCompare(b));
  const map = new Map(list.map((p, i) => [p, PROJECT_COLORS[i % PROJECT_COLORS.length]]));
  return (p) => map.get(p.trim()) ?? "#94a3b8";
}

/** "just now", "12 min ago", "6 h ago", "3 d ago" */
function ago(iso: string | null | undefined, now: number): string {
  if (!iso || !now) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (now - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** "today 16:00", "tomorrow 09:30", "Mon 07:30" */
function upcoming(iso: string | null | undefined, now: number): string {
  if (!iso) return "off";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (!now) return time;
  const today = new Date(now);
  const dayDiff = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() - new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) / 86400000);
  if (dayDiff === 0) return `today ${time}`;
  if (dayDiff === 1) return `tomorrow ${time}`;
  return `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${time}`;
}

function shortDay(iso: string): string {
  const d = parseDue(iso); // "yyyy-mm-dd" is a local calendar date (new Date() would show the previous day in Mexico)
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function Spinner({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" className="animate-spin" aria-hidden>
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14 8a6 6 0 00-6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

const BTN = "inline-flex h-7 items-center justify-center gap-1.5 rounded-md border px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] transition disabled:cursor-not-allowed disabled:opacity-50";
const BTN_AMBER = `${BTN} border-amber-400/50 bg-amber-500/15 text-amber-100 hover:bg-amber-500/25`;
const BTN_QUIET = `${BTN} border-edge-2 bg-white/[0.03] text-slate-300 hover:border-slate-500 hover:text-ink`;

/* ------------------------------------------------------------------------------------------------ status */

/** GET /argos/status, polled; `null` = the backend has no ARGOS (404), `undefined` = loading. */
export function useArgosStatus(argos: ArgosApi, ready: boolean, refreshKey: string) {
  const [status, setStatus] = useState<ArgosStatus | null | undefined>(undefined);
  const refresh = useCallback(() => {
    if (!ready) return;
    argos.status().then(setStatus);
  }, [argos, ready]);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh, refreshKey]);
  return { status, refresh };
}

/** The backend's hint for a Suite that's configured but not signed in yet. */
const needsSuiteConnect = (c: ArgosCheckStatus) => c.name === "l10" && /connect paga suite/i.test(c.hint ?? c.note ?? "");

function CheckChip({ c, now, onConnect }: { c: ArgosCheckStatus; now: number; onConnect?: () => void }) {
  // the backend's `state` is authoritative: "not configured" is a setup step, not a failure
  const raw = c.state;
  const state = !c.enabled ? "off"
    : raw === "not configured" ? "off"
    : raw === "partial" ? "partial"
    : raw === "never run" || c.last_run == null ? "never"
    : raw === "failed" || c.last_ok === false ? "failed" : "ok";
  const look = {
    ok: { color: "#22c55e", text: "ok" },
    failed: { color: "#ef4444", text: "failed" },
    partial: { color: "#f59e0b", text: "partial" },
    never: { color: "#64748b", text: "never run" },
    off: { color: "#64748b", text: "not configured" },
  }[state];
  const name = c.name === "l10" ? "L10 · PAGA Suite" : checkLabel(c.name);
  return (
    <div
      tabIndex={c.note ? 0 : undefined}
      className={cx(
        "group/chk relative flex min-w-0 items-center gap-2.5 rounded-md border px-2.5 py-1.5 outline-none focus-visible:ring-1 focus-visible:ring-signal/60",
        state === "off" ? "border-dashed border-edge-2 bg-black/20" : "border-edge bg-white/[0.02]",
        state === "failed" && "border-red-500/40 bg-red-500/[0.06]",
      )}
      aria-label={`${name}: ${look.text}${c.note ? `. ${c.note}` : ""}`}
    >
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: state === "off" ? "transparent" : look.color, boxShadow: state === "ok" ? `0 0 6px ${look.color}` : `inset 0 0 0 1.5px ${look.color}` }} />
      <div className="min-w-0 leading-tight">
        <p className="truncate font-mono text-[10.5px] font-semibold uppercase tracking-[0.12em] text-slate-200">{name}</p>
        <p className="truncate font-mono text-[10px] text-dim">
          <span style={{ color: state === "failed" ? "#fca5a5" : state === "ok" ? "#86efac" : undefined }}>{look.text}</span>
          {c.last_run && <span> · {ago(c.last_run, now)}</span>}
        </p>
      </div>
      {c.note && (
        <>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" className="shrink-0 text-mute group-hover/chk:text-slate-300" aria-hidden>
            <circle cx="8" cy="8" r="6.25" />
            <path d="M8 7.25v4M8 4.9v.1" strokeLinecap="round" />
          </svg>
          <div
            role="tooltip"
            className="pointer-events-none invisible absolute top-full left-0 z-20 mt-1.5 w-[300px] rounded-md border border-edge-2 bg-panel-2 px-3 py-2 text-[11.5px] leading-relaxed text-slate-300 opacity-0 shadow-[0_18px_40px_-12px_rgba(0,0,0,0.9)] transition-opacity group-hover/chk:visible group-hover/chk:opacity-100 group-focus-visible/chk:visible group-focus-visible/chk:opacity-100"
          >
            {c.note}
          </div>
        </>
      )}
      {onConnect && state === "off" && needsSuiteConnect(c) && (
        <button onClick={onConnect} className={cx(BTN, "h-6 border-signal/50 bg-signal/10 px-2 text-[9px] text-signal hover:bg-signal/20")} title="Sign in to PAGA Suite with Microsoft">
          Connect
        </button>
      )}
    </div>
  );
}

function StatusStrip({
  status,
  running,
  onRun,
  onConnectSuite,
  error,
  now,
}: {
  status: ArgosStatus | null | undefined;
  running: boolean;
  onRun: () => void;
  onConnectSuite?: () => void;
  error: string | null;
  now: number;
}) {
  const checks = [...(status?.checks ?? [])].sort((a, b) => (CHECK_ORDER.indexOf(a.name) + 99) % 99 - (CHECK_ORDER.indexOf(b.name) + 99) % 99);
  return (
    <section className="panel relative z-20 flex flex-col gap-2 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <div className="flex shrink-0 items-center gap-2.5">
          <h2 className="label flex items-center gap-2 !text-slate-300">
            <span className="text-signal/60">11</span>
            <span className="text-signal/30">//</span>
            Monitor
          </h2>
          <span className="font-mono text-[11px] font-semibold tracking-[0.2em] text-amber-300">ARGOS</span>
        </div>
        {status === undefined ? (
          <span className="font-mono text-[10.5px] text-mute">Loading status…</span>
        ) : status === null ? (
          <span className="font-mono text-[10.5px] text-mute">This backend has no ARGOS module (GET /argos/status → 404).</span>
        ) : (
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            {checks.map((c) => (
              <CheckChip key={c.name} c={c} now={now} onConnect={onConnectSuite} />
            ))}
          </div>
        )}
        {status && (
          <div className="ml-auto flex shrink-0 items-center gap-4">
            <dl className="grid grid-cols-[auto_auto] gap-x-2.5 gap-y-0.5 font-mono text-[10px]">
              <dt className="uppercase tracking-[0.14em] text-mute">Next run</dt>
              <dd className="text-slate-200 tabular-nums">{upcoming(status.next_run, now)}</dd>
              <dt className="uppercase tracking-[0.14em] text-mute">Next brief</dt>
              <dd className="text-slate-200 tabular-nums">{upcoming(status.next_brief, now)}</dd>
            </dl>
            <button onClick={onRun} disabled={running} className={cx(BTN_AMBER, "h-8 min-w-[150px] px-3 text-[10.5px]")}>
              {running ? (
                <>
                  <Spinner /> Running checks…
                </>
              ) : (
                "Run checks now"
              )}
            </button>
          </div>
        )}
      </div>
      {error && (
        <p className="rounded-md border border-red-500/40 bg-red-500/[0.07] px-3 py-1.5 text-[12px] text-red-200" role="status">
          {error}
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------------------------------------ alerts */

type AlertFilter = "active" | "OPEN" | "ACKNOWLEDGED" | "RESOLVED";

/** Words in `a` that `b` lacks, so a changed date or value stands out between two versions. */
function markChanges(a: string, b: string, color: string): ReactNode {
  if (a === b) return a;
  const other = new Set(b.split(/\s+/));
  const words = a.split(/\s+/).filter(Boolean);
  // Only worth marking when the two lines are mostly the same (a moved date, a changed value).
  if (!words.length || words.filter((w) => other.has(w)).length / words.length < 0.4) return a;
  return a.split(/(\s+)/).map((w, i) =>
    /\S/.test(w) && !other.has(w) ? (
      <mark key={i} className="rounded-[3px] px-0.5 text-inherit" style={{ background: `${color}30`, boxShadow: `inset 0 -1px 0 ${color}` }}>
        {w}
      </mark>
    ) : (
      w
    ),
  );
}

function Quote({ ev, label, accent, children }: { ev: AlertEvidence; label?: string; accent: string; children?: ReactNode }) {
  return (
    <figure className="flex min-w-0 flex-col gap-1">
      {label && (
        <figcaption className="font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: accent }}>
          {label}
        </figcaption>
      )}
      <blockquote className="flex-1 rounded-r-md border-l-2 bg-black/30 px-3 py-2 text-[12.5px] leading-relaxed text-slate-200" style={{ borderColor: accent }}>
        {children ?? ev.quote}
      </blockquote>
      <p className="truncate pl-3 font-mono text-[9.5px] text-dim" title={ev.source}>
        {ev.source}
      </p>
    </figure>
  );
}

function Evidence({ evidence }: { evidence: AlertEvidence[] }) {
  const prev = evidence.filter((e) => e.version === "previous");
  const cur = evidence.filter((e) => e.version === "current");
  const paired = prev.length > 0 && cur.length > 0;
  const rest = paired ? evidence.filter((e) => e.version === "single") : evidence;
  const identical = paired && prev[0].quote === cur[0].quote;
  return (
    <div className="grid gap-2">
      {paired && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Quote ev={prev[0]} label="Previous week" accent="#64748b">
            {markChanges(prev[0].quote, cur[0].quote, "#94a3b8")}
          </Quote>
          <Quote ev={cur[0]} label={identical ? "This week · identical" : "This week"} accent="#fbbf24">
            {markChanges(cur[0].quote, prev[0].quote, "#fbbf24")}
          </Quote>
          {prev.slice(1).map((e, i) => (
            <Quote key={`p${i}`} ev={e} accent="#64748b" />
          ))}
          {cur.slice(1).map((e, i) => (
            <Quote key={`c${i}`} ev={e} accent="#fbbf24" />
          ))}
        </div>
      )}
      {rest.map((e, i) => (
        <Quote key={i} ev={e} accent="#475569" />
      ))}
    </div>
  );
}

function AlertActions({ a, patch }: { a: Alert; patch: (id: string, s: Alert["status"]) => Promise<unknown> }) {
  const [busy, setBusy] = useState<Alert["status"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = (s: Alert["status"]) => {
    setBusy(s);
    setError(null);
    patch(a.id, s)
      .catch((x) => setError(apiErrorText(x, "Could not update the alert")))
      .finally(() => setBusy(null));
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      {a.status === "OPEN" && (
        <button onClick={() => act("ACKNOWLEDGED")} disabled={!!busy} className={BTN_QUIET} title="Seen it; ARGOS keeps it but stops auto-resolving it">
          {busy === "ACKNOWLEDGED" && <Spinner size={10} />} Acknowledge
        </button>
      )}
      {a.status !== "RESOLVED" ? (
        <button onClick={() => act("RESOLVED")} disabled={!!busy} className={cx(BTN, "border-emerald-400/40 bg-emerald-500/10 text-emerald-100 hover:bg-emerald-500/20")}>
          {busy === "RESOLVED" && <Spinner size={10} />} Resolve
        </button>
      ) : (
        <button onClick={() => act("OPEN")} disabled={!!busy} className={BTN_QUIET}>
          {busy === "OPEN" && <Spinner size={10} />} Reopen
        </button>
      )}
      {error && <span className="font-mono text-[10.5px] text-red-400">{error}</span>}
    </div>
  );
}

function AlertCard({ a, patch, now }: { a: Alert; patch: (id: string, s: Alert["status"]) => Promise<unknown>; now: number }) {
  const sev = SEVERITY[a.severity] ?? SEVERITY.LOW;
  const muted = a.status === "RESOLVED";
  return (
    <article className={cx("relative flex min-w-0 flex-col gap-2.5 rounded-lg border bg-black/20 p-3.5 pl-4", muted ? "border-edge/60 opacity-80" : "border-edge/80")}>
      <span className="absolute top-3 bottom-3 left-0 w-[2px] rounded-full" style={{ background: sev.color, opacity: a.status === "OPEN" ? 0.9 : 0.35 }} />
      <div className="flex flex-wrap items-center gap-1.5">
        <Tag color={sev.color} className="!text-[8.5px] font-semibold">
          {sev.label}
        </Tag>
        <Tag color="#94a3b8" className="!text-[8.5px]">
          {KIND[a.kind] ?? a.kind}
        </Tag>
        {a.status === "ACKNOWLEDGED" && (
          <Tag color="#7dd3fc" className="!text-[8.5px]">
            Acknowledged
          </Tag>
        )}
        {a.status === "RESOLVED" && (
          <Tag color="#22c55e" className="!text-[8.5px]">
            Resolved
          </Tag>
        )}
        <span className="ml-auto font-mono text-[9.5px] text-mute" title={`First seen ${new Date(a.first_seen).toLocaleString()} · last seen ${new Date(a.last_seen).toLocaleString()}`}>
          {a.first_seen === a.last_seen ? `seen ${ago(a.last_seen, now)}` : `since ${ago(a.first_seen, now).replace(" ago", "")} · seen ${ago(a.last_seen, now)}`}
        </span>
      </div>
      <div>
        <h4 className="text-[14px] leading-snug font-medium text-ink">{a.title}</h4>
        {a.detail && <p className="mt-1 text-[12.5px] leading-relaxed text-slate-400">{a.detail}</p>}
      </div>
      {a.evidence.length > 0 && <Evidence evidence={a.evidence} />}
      <AlertActions a={a} patch={patch} />
    </article>
  );
}

function ResolvedRow({ a, patch, now }: { a: Alert; patch: (id: string, s: Alert["status"]) => Promise<unknown>; now: number }) {
  const [open, setOpen] = useState(false);
  if (open)
    return (
      <div className="grid gap-1">
        <button onClick={() => setOpen(false)} className="self-start font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim hover:text-slate-200">
          ▾ Collapse
        </button>
        <AlertCard a={a} patch={patch} now={now} />
      </div>
    );
  return (
    <button onClick={() => setOpen(true)} className="flex w-full min-w-0 items-center gap-2 rounded-md border border-edge/50 bg-black/10 px-3 py-1.5 text-left transition hover:bg-white/[0.03]">
      <span className="font-mono text-[10px] text-emerald-400">✓</span>
      <span className="min-w-0 flex-1 truncate text-[12px] text-slate-400 line-through decoration-slate-600">{a.title}</span>
      <span className="shrink-0 font-mono text-[9.5px] text-mute">
        {a.project ? `${a.project} · ` : ""}
        {ago(a.last_seen, now)}
      </span>
      <span className="shrink-0 font-mono text-[10px] text-dim">▸</span>
    </button>
  );
}

function groupAlerts(list: Alert[]) {
  const by = new Map<string, Alert[]>();
  for (const a of list) {
    const k = a.project?.trim() || "";
    by.set(k, [...(by.get(k) ?? []), a]);
  }
  const worst = (xs: Alert[]) => Math.min(...xs.map((a) => SEVERITY[a.severity]?.rank ?? 9));
  const bySev = (x: Alert, y: Alert) => (SEVERITY[x.severity]?.rank ?? 9) - (SEVERITY[y.severity]?.rank ?? 9) || y.last_seen.localeCompare(x.last_seen);
  return [...by.entries()]
    .map(([project, items]) => {
      const checks = new Map<string, Alert[]>();
      for (const a of items) checks.set(a.check, [...(checks.get(a.check) ?? []), a]);
      return {
        project: project || null,
        items,
        checks: [...checks.entries()]
          .map(([check, xs]) => ({ check, items: xs.sort(bySev) }))
          .sort((a, b) => (CHECK_ORDER.indexOf(a.check) + 99) % 99 - (CHECK_ORDER.indexOf(b.check) + 99) % 99),
      };
    })
    .sort((a, b) => {
      if (!a.project !== !b.project) return a.project ? -1 : 1;
      return worst(a.items) - worst(b.items) || b.items.length - a.items.length || (a.project ?? "").localeCompare(b.project ?? "");
    });
}

function AlertsPanel({ alerts, patch, now, colorOf, className }: { alerts: Alert[]; patch: (id: string, s: Alert["status"]) => Promise<unknown>; now: number; colorOf: ColorOf; className?: string }) {
  const [filter, setFilter] = useState<AlertFilter>("active");
  const [showResolved, setShowResolved] = useState(false);
  const counts = useMemo(
    () => ({
      active: alerts.filter((a) => a.status !== "RESOLVED").length,
      OPEN: alerts.filter((a) => a.status === "OPEN").length,
      ACKNOWLEDGED: alerts.filter((a) => a.status === "ACKNOWLEDGED").length,
      RESOLVED: alerts.filter((a) => a.status === "RESOLVED").length,
    }),
    [alerts],
  );
  const shown = useMemo(
    () => alerts.filter((a) => (filter === "active" ? a.status !== "RESOLVED" : filter === "RESOLVED" ? false : a.status === filter)),
    [alerts, filter],
  );
  const groups = useMemo(() => groupAlerts(shown), [shown]);
  const resolved = useMemo(() => alerts.filter((a) => a.status === "RESOLVED").sort((a, b) => b.last_seen.localeCompare(a.last_seen)), [alerts]);
  const tabs: { id: AlertFilter; label: string }[] = [
    { id: "active", label: "Active" },
    { id: "OPEN", label: "Open" },
    { id: "ACKNOWLEDGED", label: "Acknowledged" },
    { id: "RESOLVED", label: "Resolved" },
  ];
  const high = alerts.filter((a) => a.status === "OPEN" && a.severity === "HIGH").length;

  return (
    <Panel
      code="12"
      title="Alerts"
      className={className}
      meta={
        <div className="flex flex-wrap items-center justify-end gap-2">
          {high > 0 && <span className="text-red-300">{high} open high</span>}
          <div role="radiogroup" aria-label="Alert status" className="flex items-center rounded-md border border-edge bg-black/30 p-0.5">
            {tabs.map((t) => {
              const on = filter === t.id;
              return (
                <button
                  key={t.id}
                  role="radio"
                  aria-checked={on}
                  onClick={() => setFilter(t.id)}
                  className={cx(
                    "flex h-6 items-center gap-1 rounded px-2 font-mono text-[9.5px] uppercase tracking-[0.12em] transition",
                    on ? "bg-signal/[0.12] text-ink shadow-[inset_0_0_0_1px_rgba(125,211,252,0.3)]" : "text-dim hover:text-slate-200",
                  )}
                >
                  {t.label}
                  <span className="tabular-nums tracking-normal text-mute">{counts[t.id]}</span>
                </button>
              );
            })}
          </div>
        </div>
      }
      bodyClassName="p-4"
    >
      {filter === "RESOLVED" ? (
        resolved.length ? (
          <div className="grid gap-1.5">
            {resolved.map((a) => (
              <ResolvedRow key={a.id} a={a} patch={patch} now={now} />
            ))}
          </div>
        ) : (
          <Empty>No resolved alerts yet.</Empty>
        )
      ) : groups.length === 0 ? (
        <Empty>{alerts.length ? "Nothing here with this filter." : "No alerts. ARGOS hasn't found anything that needs you."}</Empty>
      ) : (
        <div className="flex flex-col gap-5">
          {groups.map((g) => {
            const color = g.project ? colorOf(g.project) : "#64748b";
            return (
              <section key={g.project ?? "__none"}>
                <h3 className="mb-2.5 flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.18em]">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
                  <span style={{ color: g.project ? color : "#7b879f" }}>{g.project ?? "No project"}</span>
                  <span className="text-mute">· {g.items.length}</span>
                  <span className="h-px flex-1 bg-edge/70" />
                </h3>
                <div className="flex flex-col gap-3">
                  {g.checks.map((c) => (
                    <div key={c.check} className="grid gap-2">
                      <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-dim">
                        {checkLabel(c.check)} <span className="text-mute">· {c.items.length}</span>
                      </p>
                      <div className="grid gap-2.5 2xl:grid-cols-2">
                        {c.items.map((a) => (
                          <AlertCard key={a.id} a={a} patch={patch} now={now} />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
      {filter === "active" && resolved.length > 0 && (
        <div className="mt-5 border-t border-edge/60 pt-3">
          <button onClick={() => setShowResolved((v) => !v)} className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-dim hover:text-slate-200" aria-expanded={showResolved}>
            <span>{showResolved ? "▾" : "▸"}</span>
            Resolved · {resolved.length}
          </button>
          {showResolved && (
            <div className="mt-2 grid gap-1.5">
              {resolved.map((a) => (
                <ResolvedRow key={a.id} a={a} patch={patch} now={now} />
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------------------------------------------ rocks */

const twoOwners = (o: string) => /[·/,]/.test(o);

function dueText(iso: string, now: number): { text: string; late: boolean } {
  if (Number.isNaN(parseDue(iso).getTime()) || !now) return { text: "", late: false };
  const days = dayDiff(iso, now); // calendar days, local
  if (days < 0) return { text: `${-days} d overdue`, late: true };
  if (days === 0) return { text: "due today", late: false };
  if (days < 14) return { text: `in ${days} d`, late: false };
  return { text: `in ${Math.round(days / 7)} wk`, late: false };
}

const fmtNum = (x: number) => (Number.isInteger(x) ? String(x) : x.toFixed(1));

function CurrentEditor({ rock, patch }: { rock: RockStatus; patch: (id: string, current: number) => Promise<unknown> }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = () => {
    setValue(rock.current != null ? String(rock.current) : "");
    setError(null);
    setEditing(true);
  };
  const save = () => {
    const v = Number(value.replace(",", "."));
    if (value.trim() === "" || !Number.isFinite(v)) {
      setError("Enter a number");
      return;
    }
    if (v === rock.current) {
      setEditing(false);
      return;
    }
    setBusy(true);
    patch(rock.id, v)
      .then(() => setEditing(false))
      .catch((x) => setError(apiErrorText(x, "Could not update")))
      .finally(() => setBusy(false));
  };
  if (!editing)
    return (
      <button
        onClick={start}
        className="group/cur inline-flex items-center gap-1 rounded px-0.5 font-mono text-[13px] font-semibold text-ink tabular-nums underline decoration-slate-600 decoration-dotted underline-offset-4 hover:decoration-signal"
        title="Update the current value"
      >
        {rock.current != null ? fmtNum(rock.current) : "—"}
        <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-mute opacity-0 transition group-hover/cur:opacity-100 group-focus-visible/cur:opacity-100" aria-hidden>
          <path d="M11 2.5l2.5 2.5L6 12.5H3.5V10z" strokeLinejoin="round" />
        </svg>
      </button>
    );
  return (
    <span className="inline-flex items-center gap-1">
      <input
        autoFocus
        inputMode="decimal"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") setEditing(false);
        }}
        aria-label={`Current value for ${rock.title}`}
        className="h-6 w-16 rounded border border-signal/50 bg-black/50 px-1.5 font-mono text-[12px] text-ink tabular-nums focus:outline-none"
      />
      <button onClick={save} disabled={busy} className="flex h-6 w-6 items-center justify-center rounded border border-emerald-400/40 bg-emerald-500/10 text-emerald-200 hover:bg-emerald-500/20" title="Save (Enter)">
        {busy ? <Spinner size={10} /> : "✓"}
      </button>
      <button onClick={() => setEditing(false)} className="flex h-6 w-6 items-center justify-center rounded border border-edge-2 text-dim hover:text-slate-200" title="Cancel (Esc)">
        ×
      </button>
      {error && <span className="font-mono text-[10px] text-red-400">{error}</span>}
    </span>
  );
}

function Pace({ r }: { r: RockStatus }) {
  const req = r.required_pace;
  const obs = r.observed_pace;
  if (r.status === "DONE" || r.status === "UNKNOWN" || r.status === "FAILED" || req == null || obs == null) return <span className="font-mono text-[11px] text-mute">—</span>;
  const max = Math.max(req, obs, 0.0001);
  const ratio = req <= 0 ? 1 : obs / req;
  const color = ratio >= 1 ? "#22c55e" : ratio >= 0.5 ? "#f59e0b" : "#ef4444";
  const unit = "/wk";
  return (
    <div className="grid w-[168px] grid-cols-[44px_minmax(0,1fr)_44px] items-center gap-x-2 gap-y-1 font-mono text-[10px]" title={`Required ${fmtNum(req)}${unit} from today · observed ${fmtNum(obs)}${unit} so far`}>
      <span className="text-mute uppercase tracking-[0.1em]">need</span>
      <span className="h-1.5 overflow-hidden rounded-full bg-white/[0.05]">
        <span className="block h-full rounded-full bg-slate-400/70" style={{ width: `${(req / max) * 100}%` }} />
      </span>
      <span className="text-right text-slate-300 tabular-nums">{fmtNum(req)}</span>
      <span className="text-mute uppercase tracking-[0.1em]">doing</span>
      <span className="h-1.5 overflow-hidden rounded-full bg-white/[0.05]">
        <span className="block h-full rounded-full" style={{ width: `${(Math.max(0, obs) / max) * 100}%`, background: color, boxShadow: `0 0 6px ${color}66` }} />
      </span>
      <span className="text-right tabular-nums" style={{ color }}>
        {fmtNum(obs)}
      </span>
    </div>
  );
}

function RocksPanel({ rocks, patch, reload, now, colorOf, className }: { rocks: RockStatus[]; patch: (id: string, current: number) => Promise<unknown>; reload: () => Promise<unknown>; now: number; colorOf: ColorOf; className?: string }) {
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const order: RockStatus["status"][] = ["FAILED", "OFF_TRACK", "AT_RISK", "UNKNOWN", "ON_TRACK", "DONE"];
  const sorted = useMemo(() => [...rocks].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status) || a.due.localeCompare(b.due)), [rocks]); // eslint-disable-line react-hooks/exhaustive-deps
  const onTrack = rocks.filter((r) => r.status === "ON_TRACK" || r.status === "DONE").length;
  const quarters = [...new Set(rocks.map((r) => r.quarter))];
  const doReload = () => {
    setReloading(true);
    setError(null);
    reload()
      .catch((x) => setError(apiErrorText(x, "Could not reload rocks.yaml")))
      .finally(() => setReloading(false));
  };
  return (
    <Panel
      code="13"
      title={`Rocks${quarters.length === 1 ? ` · ${quarters[0]}` : ""}`}
      className={className}
      meta={
        <>
          {rocks.length > 0 && (
            <span className="text-[11px]">
              <span className={cx("font-semibold", onTrack === rocks.length ? "text-emerald-300" : onTrack * 2 >= rocks.length ? "text-amber-300" : "text-red-300")}>
                {onTrack} of {rocks.length}
              </span>{" "}
              on-track
            </span>
          )}
          <button onClick={doReload} disabled={reloading} className={cx(BTN_QUIET, "h-6 px-2 text-[9px]")} title="Re-read rocks.yaml">
            {reloading ? <Spinner size={10} /> : "↻"} Reload
          </button>
        </>
      }
    >
      {error && <p className="mx-4 mt-3 font-mono text-[11px] text-red-400">{error}</p>}
      {rocks.length === 0 ? (
        <Empty>No Rocks yet. Add them to rocks.yaml under ATLAS_LOCAL_DIR/argos/ and reload.</Empty>
      ) : (
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full min-w-[980px] border-collapse text-left">
            <thead>
              <tr className="border-b border-edge/70 font-mono text-[9px] uppercase tracking-[0.16em] text-mute">
                <th className="py-2 pr-3 pl-4 font-normal">Status</th>
                <th className="px-3 py-2 font-normal">Rock</th>
                <th className="px-3 py-2 font-normal">Owner</th>
                <th className="px-3 py-2 font-normal">Due</th>
                <th className="px-3 py-2 font-normal">Measurable · current / target</th>
                <th className="px-3 py-2 font-normal">Pace per week</th>
                <th className="py-2 pr-4 pl-3 font-normal">Reason</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const s = ROCK_STATUS[r.status] ?? ROCK_STATUS.UNKNOWN;
                const due = dueText(r.due, now);
                const measurable = r.metric != null && r.target != null;
                const progress = measurable && r.current != null && r.target ? Math.max(0, Math.min(1, r.current / r.target)) : 0;
                return (
                  <tr key={r.id} className="border-b border-edge/40 align-middle last:border-b-0 hover:bg-white/[0.015]">
                    <td className="py-3 pr-3 pl-4">
                      <span
                        className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[9.5px] whitespace-nowrap uppercase tracking-[0.12em]"
                        style={{ color: s.color, borderColor: `${s.color}55`, background: `${s.color}14` }}
                      >
                        <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.color }} />
                        {s.label}
                      </span>
                    </td>
                    <td className="max-w-[300px] px-3 py-3">
                      <p className="text-[13px] leading-snug text-ink">{r.title}</p>
                      {r.project && (
                        <p className="mt-0.5 font-mono text-[9.5px] uppercase tracking-[0.12em]" style={{ color: colorOf(r.project) }}>
                          {r.project}
                        </p>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <p className="text-[12px] whitespace-nowrap text-slate-300">{r.owner}</p>
                      {twoOwners(r.owner) && (
                        <p className="mt-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-amber-300" title="A Rock with two owners has no owner">
                          2 owners
                        </p>
                      )}
                    </td>
                    <td className="px-3 py-3 whitespace-nowrap">
                      <p className="font-mono text-[11.5px] text-slate-300">{shortDay(r.due)}</p>
                      {r.status !== "DONE" && <p className={cx("font-mono text-[9.5px]", due.late ? "text-red-300" : "text-mute")}>{due.text}</p>}
                    </td>
                    <td className="px-3 py-3">
                      {measurable ? (
                        <div className="w-[220px]">
                          <div className="flex items-baseline gap-1">
                            <CurrentEditor rock={r} patch={patch} />
                            <span className="font-mono text-[11px] text-dim tabular-nums">/ {fmtNum(r.target!)}</span>
                            <span className="ml-1 truncate text-[11px] text-dim">{r.metric}</span>
                          </div>
                          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/[0.06]">
                            <div className="h-full rounded-full transition-[width] duration-700" style={{ width: `${progress * 100}%`, background: s.color, boxShadow: progress ? `0 0 6px ${s.color}66` : undefined }} />
                          </div>
                        </div>
                      ) : r.status === "DONE" ? (
                        <span className="font-mono text-[11px] text-dim">—</span>
                      ) : (
                        <span className="inline-flex items-center rounded border border-dashed border-slate-600 px-2 py-0.5 font-mono text-[10px] text-slate-400">needs a measurable</span>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <Pace r={r} />
                    </td>
                    <td className="max-w-[260px] py-3 pr-4 pl-3 text-[12px] leading-snug text-slate-400">{r.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------------------------------------------ brief */

function BriefCard({
  briefs,
  fileUrl,
  build,
  building,
  error,
  className,
}: {
  /** newest first */
  briefs: Brief[];
  fileUrl: (b: Brief) => string | undefined;
  build: () => void;
  building: boolean;
  error: string | null;
  className?: string;
}) {
  const [picked, setPicked] = useState<string | null>(null);
  const latest = briefs[0] ?? null;
  const brief = briefs.find((b) => b.id === picked) ?? latest;
  useEffect(() => setPicked(null), [latest?.id]);
  const href = brief ? fileUrl(brief) : undefined;
  return (
    <Panel
      code="14"
      title="L10 brief"
      className={className}
      meta={
        <button onClick={build} disabled={building} className={cx(BTN_AMBER, "h-6 px-2 text-[9px]")}>
          {building ? (
            <>
              <Spinner size={10} /> Building…
            </>
          ) : (
            "Build now"
          )}
        </button>
      }
      bodyClassName="flex flex-col"
    >
      {error && <p className="mx-4 mt-3 rounded-md border border-red-500/40 bg-red-500/[0.07] px-3 py-1.5 text-[12px] text-red-200">{error}</p>}
      {!brief ? (
        <Empty>{building ? "ARGOS is drafting the brief…" : "No brief yet. It's built on Monday mornings, or now with Build now."}</Empty>
      ) : (
        <div className="flex flex-col gap-3 p-4">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] uppercase tracking-[0.16em] text-dim">
            <span className="text-amber-300">{brief.week}</span>
            <span className="text-mute">·</span>
            <span className="normal-case tracking-normal">
              {new Date(brief.created_at).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })}
            </span>
            {brief.id !== latest?.id && (
              <button onClick={() => setPicked(null)} className="ml-auto text-signal hover:underline">
                Back to latest →
              </button>
            )}
          </div>
          {brief.headline.length > 0 && (
            <ul className="grid gap-1.5 rounded-lg border border-amber-400/25 bg-gradient-to-br from-amber-500/[0.07] via-transparent to-transparent px-3.5 py-3">
              {brief.headline.slice(0, 3).map((h, i) => (
                <li key={i} className="flex gap-2.5 text-[13.5px] leading-snug text-ink">
                  <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-amber-300" />
                  <span className="min-w-0">{h}</span>
                </li>
              ))}
            </ul>
          )}
          {Object.entries(brief.sections).map(([name, lines]) => (
            <div key={name}>
              <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-dim">{name}</p>
              <ul className="mt-1 grid gap-0.5">
                {lines.map((l, i) => (
                  <li key={i} className="flex gap-2 text-[12px] leading-relaxed text-slate-300">
                    <span className="mt-[8px] h-1 w-1 shrink-0 rounded-full bg-slate-500" />
                    <span className="min-w-0">{l}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {href && (
            <a href={href} download={brief.deliverable?.name ?? `L10 brief ${brief.week}.docx`} className={cx(BTN_QUIET, "self-start text-signal")}>
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M8 2.5v8M4.5 7L8 10.5 11.5 7M3 13.5h10" />
              </svg>
              Download .docx
            </a>
          )}
        </div>
      )}
      {briefs.length > 1 && (
        <div className="mt-auto border-t border-edge/60 px-4 py-2.5">
          <p className="label !text-[8.5px]">Past briefs · {briefs.length}</p>
          <ul className="mt-1 grid gap-0.5">
            {briefs.map((b, i) => (
              <li key={b.id}>
                <button
                  onClick={() => setPicked(i === 0 ? null : b.id)}
                  className={cx("flex w-full items-center gap-2 rounded px-1.5 py-1 text-left font-mono text-[10.5px] transition hover:bg-white/[0.03]", b.id === brief?.id ? "text-ink" : "text-slate-400")}
                >
                  <span className={cx("h-1 w-1 rounded-full", b.id === brief?.id ? "bg-amber-300" : "bg-slate-600")} />
                  {b.week}
                  <span className="text-mute">{new Date(b.created_at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })}</span>
                  {i === 0 && <span className="ml-auto text-[9px] uppercase tracking-[0.14em] text-amber-300/80">latest</span>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------------------------------------------ view */

export function MonitorView({
  alerts,
  rocks,
  briefs,
  missions,
  argos,
  ready,
  feed,
}: {
  alerts: Alert[];
  rocks: RockStatus[];
  /** newest first */
  briefs: Brief[];
  missions: Mission[];
  argos: ArgosApi;
  ready: boolean;
  /** the Activity feed for this view */
  feed: ReactNode;
}) {
  const now = useNow(30_000);

  /* run tracking: POST /argos/run → a mission; done when that mission closes */
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [briefId, setBriefId] = useState<string | null>(null);
  const [briefStarting, setBriefStarting] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);
  const closedKey = [runId, briefId]
    .map((id) => (id ? missions.find((m) => m.id === id) : undefined))
    .map((m) => (m ? `${m.id}:${m.phase}` : ""))
    .join("|");
  const { status, refresh } = useArgosStatus(argos, ready, closedKey);
  const statusMission = status?.mission_id ? missions.find((m) => m.id === status.mission_id) : undefined;
  // A run someone else started (the scheduler) is visible through status.running; refresh when its mission closes.
  useEffect(() => {
    if (statusMission && (statusMission.phase === "CLOSED" || statusMission.interrupted)) refresh();
  }, [statusMission?.phase, statusMission?.interrupted]); // eslint-disable-line react-hooks/exhaustive-deps

  const isClosed = (id: string | null) => {
    const m = id ? missions.find((x) => x.id === id) : undefined;
    return !!m && (m.phase === "CLOSED" || m.interrupted);
  };
  useEffect(() => {
    if (runId && isClosed(runId)) setRunId(null);
    if (briefId && isClosed(briefId)) setBriefId(null);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const run = (checks?: string[]) => {
    setStarting(true);
    setRunError(null);
    argos
      .run(checks)
      .then((m) => {
        setRunId(m.id);
        refresh();
      })
      .catch((x) => setRunError(apiErrorText(x, "Could not start the checks")))
      .finally(() => setStarting(false));
  };
  const build = () => {
    setBriefStarting(true);
    setBriefError(null);
    argos
      .buildBrief()
      .then((m) => setBriefId(m.id))
      .catch((x) => setBriefError(apiErrorText(x, "Could not build the brief")))
      .finally(() => setBriefStarting(false));
  };
  const running = starting || !!runId || !!status?.running;

  /* PAGA Suite sign-in (device code); on success run just the L10 check */
  const [suiteConnecting, setSuiteConnecting] = useState(false);
  const closeSuite = useCallback(() => setSuiteConnecting(false), []);
  const onSuiteConnected = useCallback(() => {
    refresh();
    run(["l10"]);
  }, [refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const colorOf = useMemo(() => projectPalette([...alerts.map((a) => a.project), ...rocks.map((r) => r.project)]), [alerts, rocks]);
  const patchAlert = useCallback((id: string, s: Alert["status"]) => argos.patchAlert(id, s), [argos]);
  const patchRock = useCallback((id: string, current: number) => argos.patchRock(id, current), [argos]);

  return (
    <div className="flex flex-col gap-4">
      <StatusStrip status={status} running={running} onRun={() => run()} onConnectSuite={() => setSuiteConnecting(true)} error={runError} now={now} />
      {suiteConnecting &&
        createPortal(
          <DeviceCodeModal start={argos.suiteConnect} poll={argos.suiteConnectStatus} copy={SUITE_CONNECT_COPY} onClose={closeSuite} onConnected={onSuiteConnected} closeOnConnected />,
          document.body,
        )}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px] 2xl:grid-cols-[minmax(0,1fr)_420px]">
        <AlertsPanel alerts={alerts} patch={patchAlert} now={now} colorOf={colorOf} className="min-h-[420px]" />
        <div className="flex min-w-0 flex-col gap-4 xl:self-start">
          <BriefCard briefs={briefs} fileUrl={argos.briefFileUrl} build={build} building={briefStarting || !!briefId} error={briefError} />
          {feed}
        </div>
      </div>
      <RocksPanel rocks={rocks} patch={patchRock} reload={argos.reloadRocks} now={now} colorOf={colorOf} />
    </div>
  );
}
