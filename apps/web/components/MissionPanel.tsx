"use client";

import { useEffect, useState } from "react";
import type { Scenario } from "@/lib/api";
import type { Mission, MissionPhase, NodeDefinition, Task } from "@/lib/contracts";
import { PHASES, PRIORITY, cx, elapsed, human, useNow } from "@/lib/ui";
import { Tag } from "./primitives";

/* ---------------------------------------------------------------- phase rail */

export function PhaseRail({ phase, dim }: { phase: MissionPhase | null; dim?: boolean }) {
  const closed = phase === "CLOSED";
  const idx = phase ? (closed ? PHASES.length : PHASES.indexOf(phase)) : -1;
  return (
    <ol className={cx("grid w-full grid-cols-9", dim && "opacity-40")}>
      {PHASES.map((p, i) => {
        const done = i < idx;
        const current = i === idx;
        const color = done ? "#7dd3fc" : current ? "#22c55e" : "#26324f";
        return (
          <li key={p} className="relative flex flex-col items-center">
            {/* connector */}
            {i > 0 && (
              <span className="absolute top-[7px] right-1/2 h-px w-full" style={{ background: i <= idx ? "linear-gradient(90deg,#7dd3fc88,#7dd3fc)" : "#1f2a45" }} />
            )}
            <span className="relative z-10 flex h-[15px] w-[15px] items-center justify-center">
              {current && <span className="phase-ping absolute inset-0 rounded-full" style={{ background: color }} />}
              <span
                className="relative h-[11px] w-[11px] rounded-full border"
                style={{
                  borderColor: color,
                  background: done ? color : current ? `${color}` : "#0a0f1c",
                  boxShadow: current ? `0 0 12px ${color}` : done ? `0 0 6px ${color}66` : undefined,
                }}
              />
            </span>
            <span className={cx("mt-2 font-mono text-[9px] tracking-[0.16em]", current ? "text-emerald-300" : done ? "text-sky-200/80" : "text-mute")}>
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className={cx("mt-0.5 max-w-full truncate px-0.5 font-mono text-[9.5px] uppercase tracking-[0.1em]", current ? "text-ink" : done ? "hidden text-slate-400 lg:block" : "hidden text-mute lg:block")}>
              {human(p)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/* ---------------------------------------------------------------- launch form */

export function LaunchForm({
  node,
  loadScenarios,
  launch,
  onLaunched,
  onCancel,
  hero,
}: {
  node: NodeDefinition | null;
  loadScenarios: () => Promise<Scenario[]>;
  launch: (b: { objective: string; node: string; scenario_id?: string }) => Promise<Mission>;
  onLaunched: (m: Mission) => void;
  onCancel?: () => void;
  hero?: boolean;
}) {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenarioId, setScenarioId] = useState("");
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    loadScenarios()
      .then((list) => {
        if (!live) return;
        const mine = list.filter((s) => !node || s.node === node.id);
        setScenarios(mine);
        if (mine[0]) setScenarioId((cur) => cur || mine[0].id);
      })
      .catch(() => live && setScenarios([]));
    return () => {
      live = false;
    };
  }, [loadScenarios, node]);

  const scenario = scenarios.find((s) => s.id === scenarioId);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!node) return;
    const obj = objective.trim() || scenario?.objective || "";
    if (!obj) {
      setErr("Describe the objective.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const m = await launch({ objective: obj, node: node.id, scenario_id: scenarioId || undefined });
      setObjective("");
      onLaunched(m);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Launch failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <label className="flex flex-col gap-1.5">
        <span className="label">Objective</span>
        <textarea
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          rows={hero ? 3 : 2}
          placeholder={scenario?.objective ?? "What should the organization achieve?"}
          className="resize-none rounded-md border border-edge bg-black/30 px-3 py-2 text-[13px] leading-relaxed text-ink placeholder:text-mute focus:border-signal/60 focus:outline-none focus:ring-1 focus:ring-signal/30"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e as unknown as React.FormEvent);
          }}
        />
      </label>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span className="label">Scenario</span>
          <select
            value={scenarioId}
            onChange={(e) => setScenarioId(e.target.value)}
            className="h-9 min-w-0 rounded-md border border-edge bg-black/30 px-2.5 font-mono text-[11.5px] text-slate-200 focus:border-signal/60 focus:outline-none"
          >
            {scenarios.length === 0 && <option value="">Default scenario for {node?.name ?? "node"}</option>}
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
        </label>
        {onCancel && (
          <button type="button" onClick={onCancel} className="h-9 rounded-md px-3 font-mono text-[11px] uppercase tracking-[0.18em] text-dim hover:text-slate-200">
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={busy || !node}
          className="group flex h-9 items-center gap-2 rounded-md border border-signal/50 bg-signal/10 px-4 font-mono text-[11px] font-semibold uppercase tracking-[0.22em] text-signal shadow-[0_0_24px_-8px_#7dd3fc] transition hover:bg-signal/20 disabled:opacity-50"
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="M3 8h9M8.5 4l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {busy ? "Launching" : "Launch mission"}
        </button>
      </div>
      {err && <p className="font-mono text-[11px] text-red-400">{err}</p>}
    </form>
  );
}

/* ---------------------------------------------------------------- mission panel */

export function MissionPanel({
  mission,
  missions,
  node,
  tasks,
  activeAgents,
  totalAgents,
  pendingApprovals,
  onSelectMission,
  loadScenarios,
  launch,
  onLaunched,
}: {
  mission: Mission | null;
  missions: Mission[];
  node: NodeDefinition | null;
  tasks: Task[];
  activeAgents: number;
  totalAgents: number;
  pendingApprovals: number;
  onSelectMission: (id: string) => void;
  loadScenarios: () => Promise<Scenario[]>;
  launch: (b: { objective: string; node: string; scenario_id?: string }) => Promise<Mission>;
  onLaunched: (m: Mission) => void;
}) {
  const [composing, setComposing] = useState(false);
  const now = useNow(1000);

  if (!mission) {
    return (
      <section className="panel overflow-hidden">
        <div className="grid gap-8 p-6 lg:grid-cols-[1.1fr_1fr] lg:p-8">
          <div className="flex flex-col justify-center">
            <p className="label !text-signal/70">No active mission · {node?.name ?? "—"} node</p>
            <h1 className="mt-3 text-[28px] font-light leading-tight tracking-tight text-ink">
              The team is <span className="font-semibold text-signal">standing by</span>.
            </h1>
            <p className="mt-3 max-w-xl text-[13.5px] leading-relaxed text-slate-400">
              Give ATLAS an objective. It will decompose it, delegate to its specialists, supervise their collaboration and hand you a consolidated
              report — pausing for your approval before anything consequential happens.
            </p>
            <div className="mt-6 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] uppercase tracking-[0.22em] text-dim">
              {["Task", "Delegate", "Collaborate", "Review", "Report"].map((w, i) => (
                <span key={w} className="flex items-center gap-2">
                  {i > 0 && <span className="text-signal/40">→</span>}
                  <span className={i === 0 ? "text-signal" : undefined}>{w}</span>
                </span>
              ))}
            </div>
          </div>
          <div className="rounded-lg border border-edge bg-black/20 p-5">
            <LaunchForm hero node={node} loadScenarios={loadScenarios} launch={launch} onLaunched={onLaunched} />
          </div>
        </div>
      </section>
    );
  }

  const done = tasks.filter((t) => t.status === "COMPLETED").length;
  const closed = mission.phase === "CLOSED";
  const pr = PRIORITY[mission.priority];

  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-col gap-5 px-5 pt-4 pb-5 xl:flex-row xl:items-start">
        {/* objective */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="label !text-signal/70">Active mission</span>
            <span className="font-mono text-[10px] text-mute">{mission.id}</span>
            <Tag color={node?.color ?? "#7dd3fc"}>{node?.name ?? mission.node}</Tag>
            <Tag color={pr.color}>{pr.label}</Tag>
            {closed ? <Tag color="#cbd5e1">Closed</Tag> : <Tag color="#22c55e">In flight</Tag>}
            {missions.length > 1 && (
              <select
                value={mission.id}
                onChange={(e) => onSelectMission(e.target.value)}
                className="ml-1 h-6 rounded border border-edge bg-black/30 px-1.5 font-mono text-[10px] text-dim focus:outline-none"
                aria-label="Mission history"
              >
                {missions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.objective.slice(0, 48)}
                    {m.objective.length > 48 ? "…" : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
          <h1 className="mt-2 line-clamp-2 text-[19px] leading-snug font-normal tracking-tight text-ink">{mission.objective}</h1>
        </div>

        {/* stats + action */}
        <div className="flex shrink-0 items-stretch gap-2">
          <Stat label="Elapsed" value={now ? elapsed(mission.created_at, closed && mission.closed_at ? new Date(mission.closed_at).getTime() : now) : "--:--"} />
          <Stat label="Tasks" value={`${done}/${tasks.length}`} />
          <Stat label="Agents active" value={`${activeAgents}/${totalAgents}`} />
          <Stat label="Approvals" value={String(pendingApprovals)} alert={pendingApprovals > 0} />
          <button
            onClick={() => setComposing((v) => !v)}
            className={cx(
              "ml-1 flex items-center gap-2 rounded-md border px-3.5 font-mono text-[10.5px] font-semibold uppercase tracking-[0.2em] transition",
              composing ? "border-edge-2 text-dim" : "border-signal/50 bg-signal/10 text-signal hover:bg-signal/20",
            )}
          >
            <span className="text-[14px] leading-none">{composing ? "×" : "+"}</span> New mission
          </button>
        </div>
      </div>

      {composing && (
        <div className="mx-5 mb-5 rounded-lg border border-edge bg-black/25 p-4">
          <LaunchForm
            node={node}
            loadScenarios={loadScenarios}
            launch={launch}
            onCancel={() => setComposing(false)}
            onLaunched={(m) => {
              setComposing(false);
              onLaunched(m);
            }}
          />
        </div>
      )}

      <div className="border-t border-edge/70 bg-black/15 px-5 pt-3.5 pb-3">
        <PhaseRail phase={mission.phase} />
      </div>
    </section>
  );
}

function Stat({ label, value, alert }: { label: string; value: string; alert?: boolean }) {
  return (
    <div
      className={cx("flex min-w-[84px] flex-col justify-center rounded-md border px-3 py-1.5", alert ? "border-amber-500/50 bg-amber-500/10" : "border-edge bg-black/20")}
    >
      <span className="font-mono text-[8.5px] uppercase tracking-[0.2em] text-dim">{label}</span>
      <span className={cx("mt-0.5 font-mono text-[16px] tabular-nums", alert ? "text-amber-300" : "text-ink")}>{value}</span>
    </div>
  );
}
