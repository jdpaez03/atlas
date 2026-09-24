"use client";

import { useEffect, useMemo, useState } from "react";
import { apiErrorText, type UsageReport } from "@/lib/api";
import type { AgentDefinition, Usage } from "@/lib/contracts";
import { cx, fmtTokens, fmtUsd } from "@/lib/ui";
import { Empty, Panel } from "./primitives";

/* ---------------------------------------------------------------- per-agent usage (a mission or a window) */

type Row = { id: string; agent?: AgentDefinition; u: Usage };

function rowsOf(byAgent: Record<string, Usage> | undefined, agents: Map<string, AgentDefinition>): Row[] {
  return Object.entries(byAgent ?? {})
    .map(([id, u]) => ({ id, agent: agents.get(id), u }))
    .filter((r) => r.u.llm_calls > 0 || r.u.est_cost_usd > 0)
    .sort((a, b) => b.u.est_cost_usd - a.u.est_cost_usd || b.u.llm_calls - a.u.llm_calls);
}

/**
 * Usage by agent: name + color, calls, tokens, ~$ cost, sorted by cost, with a thin bar per agent
 * (share of the largest spender). Renders nothing when there is no per-agent usage.
 */
export function UsageByAgent({ byAgent, agents, className }: { byAgent: Record<string, Usage> | undefined; agents: Map<string, AgentDefinition>; className?: string }) {
  const rows = rowsOf(byAgent, agents);
  if (!rows.length) return null;
  const max = Math.max(...rows.map((r) => r.u.est_cost_usd), 1e-9);
  const total = rows.reduce((a, r) => a + r.u.est_cost_usd, 0);
  return (
    <div className={cx("grid gap-px overflow-hidden rounded-md border border-edge/60", className)}>
      <div className="grid grid-cols-[minmax(0,1fr)_44px_64px_64px] items-center gap-3 bg-black/25 px-3 py-1.5 font-mono text-[8.5px] uppercase tracking-[0.2em] text-mute sm:grid-cols-[minmax(0,1fr)_44px_64px_64px_48px]">
        <span>Agent</span>
        <span className="text-right">Calls</span>
        <span className="text-right">Tokens</span>
        <span className="text-right">~Cost</span>
        <span className="hidden text-right sm:block">Share</span>
      </div>
      {rows.map(({ id, agent, u }) => {
        const color = agent?.color ?? "#94a3b8";
        const tokens = u.input_tokens + u.output_tokens;
        return (
          <div key={id} className="bg-black/15 px-3 pt-1.5 pb-1">
            <div className="grid grid-cols-[minmax(0,1fr)_44px_64px_64px] items-baseline gap-3 font-mono text-[11px] sm:grid-cols-[minmax(0,1fr)_44px_64px_64px_48px]">
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="h-1.5 w-1.5 shrink-0 self-center rounded-full" style={{ background: color }} />
                <span className="truncate font-semibold tracking-[0.08em]" style={{ color }}>
                  {agent?.name ?? id.toUpperCase()}
                </span>
                {agent?.title && <span className="hidden truncate text-[9.5px] tracking-normal text-mute md:inline">{agent.title}</span>}
              </span>
              <span className="text-right tabular-nums text-slate-300">{u.llm_calls}</span>
              <span
                className="text-right tabular-nums text-slate-300"
                title={`${u.input_tokens.toLocaleString()} in · ${u.output_tokens.toLocaleString()} out${u.cache_read_tokens ? ` · ${u.cache_read_tokens.toLocaleString()} cache-read` : ""}`}
              >
                {fmtTokens(tokens)}
              </span>
              <span className="text-right tabular-nums text-emerald-300">{fmtUsd(u.est_cost_usd)}</span>
              <span className="hidden text-right tabular-nums text-dim sm:block">{total > 0 ? `${Math.round((u.est_cost_usd / total) * 100)}%` : "—"}</span>
            </div>
            <div className="mt-1 h-[3px] w-full overflow-hidden rounded-full bg-white/[0.04]">
              <div className="h-full rounded-full" style={{ width: `${Math.max(1.5, (u.est_cost_usd / max) * 100)}%`, background: `linear-gradient(90deg, ${color}55, ${color})` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---------------------------------------------------------------- the Usage view (GET /usage) */

const RANGES = [7, 30, 90] as const;
type Range = (typeof RANGES)[number];

/** Every date in the window (UTC, oldest → newest), so gaps show as empty days. */
function windowDays(days: number): string[] {
  const out: string[] = [];
  const today = Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate());
  for (let i = days - 1; i >= 0; i--) out.push(new Date(today - i * 86_400_000).toISOString().slice(0, 10));
  return out;
}

const shortDate = (d: string) => {
  const t = new Date(`${d}T00:00:00Z`);
  return Number.isNaN(t.getTime()) ? d : t.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" });
};

function Tile({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="flex min-w-[118px] flex-1 flex-col justify-center rounded-md border border-edge bg-black/20 px-3 py-2">
      <span className="font-mono text-[8.5px] uppercase tracking-[0.2em] text-dim">{label}</span>
      <span className="mt-0.5 font-mono text-[18px] tabular-nums" style={{ color: accent ?? "#e6edf7" }}>
        {value}
      </span>
      {sub && <span className="font-mono text-[9.5px] text-mute">{sub}</span>}
    </div>
  );
}

function DayBars({ report }: { report: UsageReport }) {
  const days = useMemo(() => windowDays(report.days), [report.days]);
  const byDate = useMemo(() => new Map(report.by_day.map((d) => [d.date, d])), [report.by_day]);
  const max = Math.max(...report.by_day.map((d) => d.est_cost_usd), 1e-9);
  const active = report.by_day.filter((d) => d.llm_calls > 0 || d.est_cost_usd > 0).length;
  const color = "#34d399";
  return (
    <section className="rounded-lg border border-edge/70 bg-black/15 p-3">
      <h4 className="mb-3 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em] text-emerald-300/80">
        Cost per day
        <span className="text-mute">· {active} active day{active === 1 ? "" : "s"}</span>
        <span className="ml-auto normal-case tracking-normal text-mute">max {fmtUsd(report.by_day.length ? max : 0)}</span>
      </h4>
      <div className="flex h-36 items-end gap-[2px]" role="img" aria-label={`Estimated cost per day over the last ${report.days} days`}>
        {days.map((d) => {
          const v = byDate.get(d);
          const h = v && v.est_cost_usd > 0 ? Math.max(2, (v.est_cost_usd / max) * 100) : 0;
          return (
            <div
              key={d}
              className="group relative flex h-full min-w-0 flex-1 items-end"
              title={`${shortDate(d)} · ${v ? `${fmtUsd(v.est_cost_usd)} · ${v.llm_calls} call${v.llm_calls === 1 ? "" : "s"}` : "no usage"}`}
            >
              <div className="w-full rounded-t-[2px] bg-white/[0.03]" style={{ height: h ? undefined : "2px" }} />
              {h > 0 && (
                <div
                  className="absolute inset-x-0 bottom-0 rounded-t-[2px] transition-opacity group-hover:opacity-100"
                  style={{ height: `${h}%`, background: `linear-gradient(180deg, ${color}, ${color}55)`, opacity: 0.8 }}
                />
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 flex justify-between font-mono text-[9px] tabular-nums text-mute">
        <span>{shortDate(days[0])}</span>
        {days.length > 14 && <span>{shortDate(days[Math.floor(days.length / 2)])}</span>}
        <span>{shortDate(days[days.length - 1])}</span>
      </div>
    </section>
  );
}

export function UsageView({
  load,
  node,
  nodeName,
  agents,
  ready,
  refreshKey,
}: {
  load: (days: number, node: string | null) => Promise<UsageReport | null>;
  node: string | null;
  nodeName: string;
  agents: Map<string, AgentDefinition>;
  ready: boolean;
  /** changes when a refetch makes sense (connection, missions closing) */
  refreshKey?: string;
}) {
  const [days, setDays] = useState<Range>(30);
  const [report, setReport] = useState<UsageReport | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!ready) return;
    let alive = true;
    setLoading(true);
    setErr(null);
    load(days, node)
      .then((r) => alive && setReport(r))
      .catch((x) => alive && setErr(apiErrorText(x, "Couldn't load usage")))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [load, days, node, ready, refreshKey]);

  const t = report?.totals;
  return (
    <Panel
      code="U1"
      title="Usage"
      meta={
        <div className="flex items-center gap-3">
          <span className="hidden sm:inline">
            {nodeName} node{loading ? " · loading…" : ""}
          </span>
          <div role="radiogroup" aria-label="Window" className="flex items-center gap-1">
            {RANGES.map((r) => {
              const on = r === days;
              return (
                <button
                  key={r}
                  role="radio"
                  aria-checked={on}
                  onClick={() => setDays(r)}
                  className={cx(
                    "rounded border px-1.5 py-[1px] font-mono text-[10px] tabular-nums transition",
                    on ? "border-signal/60 bg-signal/15 text-signal" : "border-edge-2 text-dim hover:text-slate-200",
                  )}
                >
                  {r}d
                </button>
              );
            })}
          </div>
        </div>
      }
    >
      {err ? (
        <Empty className="min-h-40 !text-red-300/90">{err}</Empty>
      ) : report === undefined ? (
        <Empty className="min-h-40">Loading usage…</Empty>
      ) : report === null ? (
        <Empty className="min-h-40">This ATLAS backend has no usage endpoint (GET /usage). Update the API to see usage over time.</Empty>
      ) : (
        <div className="grid gap-4 p-4">
          <div>
            <div className="flex flex-wrap gap-2">
              <Tile label="Est. cost" value={fmtUsd(t!.est_cost_usd)} accent="#6ee7b7" sub={report.backend === "subscription" ? "API-equivalent" : report.backend === "api" ? "Claude API" : undefined} />
              <Tile label="LLM calls" value={t!.llm_calls.toLocaleString()} />
              <Tile label="Input tokens" value={fmtTokens(t!.input_tokens)} />
              <Tile label="Output tokens" value={fmtTokens(t!.output_tokens)} />
              <Tile label="Cache-read" value={fmtTokens(t!.cache_read_tokens)} />
              <Tile label="Missions" value={String(report.missions)} sub={`last ${report.days} days`} />
            </div>
            {report.note && <p className="mt-2 font-mono text-[10px] leading-relaxed tracking-[0.04em] text-mute">{report.note}</p>}
          </div>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
            <section className="min-w-0">
              <h4 className="mb-2 font-mono text-[10px] uppercase tracking-[0.2em] text-signal/70">By agent</h4>
              {Object.keys(report.by_agent).length ? (
                <UsageByAgent byAgent={report.by_agent} agents={agents} />
              ) : (
                <p className="text-[11.5px] text-mute">No LLM usage in this window.</p>
              )}
            </section>
            <DayBars report={report} />
          </div>
        </div>
      )}
    </Panel>
  );
}
