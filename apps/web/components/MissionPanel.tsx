"use client";

import { useEffect, useState } from "react";
import { apiErrorText, type AtlasConfig, type LaunchMissionBody, type MissionMode, type Scenario } from "@/lib/api";
import type { AgentDefinition, Mission, MissionPhase, NodeDefinition, Task, Usage } from "@/lib/contracts";
import { PHASES, PRIORITY, cx, elapsed, fmtTokens, fmtUsd, human, useNow } from "@/lib/ui";
import { DropZone, PendingFiles, acceptFiles } from "./Files";
import { Tag } from "./primitives";
import { UsageByAgent } from "./Usage";

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

type LaunchFn = (b: LaunchMissionBody, files?: File[]) => Promise<Mission>;

const shortModel = (m?: string | null) => (m ? m.replace(/^claude-/, "").replace(/-\d{8}$/, "") : "—");

function BackendBadge({ backend, costBasis }: { backend: "api" | "subscription"; costBasis?: AtlasConfig["cost_basis"] }) {
  const plan = backend === "subscription";
  return (
    <span
      className={cx(
        "rounded border px-1 py-[1px] tracking-[0.12em]",
        plan ? "border-violet-400/40 text-violet-300/90" : "border-slate-500/40 text-slate-300/90",
      )}
      title={plan ? "Runs on your Claude Max plan via Claude Code; cost shown is the API-equivalent value, not billed" : "Runs on the Claude API (billed per token)"}
    >
      {plan ? "Max plan" : "API"}
      <span className="text-mute"> · {costBasis === "api_equivalent" ? "API-equivalent cost" : "API cost"}</span>
    </span>
  );
}

function ModeSwitch({ mode, onMode, liveAvailable }: { mode: MissionMode; onMode: (m: MissionMode) => void; liveAvailable: boolean }) {
  const opt = (m: MissionMode, label: string, disabled: boolean) => {
    const on = mode === m;
    const live = m === "live";
    return (
      <button
        type="button"
        role="radio"
        aria-checked={on}
        disabled={disabled}
        onClick={() => onMode(m)}
        title={disabled ? "Add ANTHROPIC_API_KEY to .env to enable live agents" : undefined}
        className={cx(
          "flex h-7 items-center gap-1.5 rounded px-3 font-mono text-[10px] font-semibold uppercase tracking-[0.2em] transition",
          on ? (live ? "bg-emerald-500/15 text-emerald-300 shadow-[inset_0_0_0_1px_#34d39966]" : "bg-signal/10 text-signal shadow-[inset_0_0_0_1px_#7dd3fc55]") : "text-dim hover:text-slate-200",
          disabled && "cursor-not-allowed opacity-40 hover:text-dim",
        )}
      >
        {live && <span className={cx("h-1.5 w-1.5 rounded-full", on ? "bg-emerald-400 dot-live" : "bg-current")} style={{ ["--c" as string]: "#34d399aa" }} />}
        {label}
      </button>
    );
  };
  return (
    <div role="radiogroup" aria-label="Mission mode" className="inline-flex rounded-md border border-edge bg-black/30 p-0.5">
      {opt("live", "Live", !liveAvailable)}
      {opt("simulated", "Simulated", false)}
    </div>
  );
}

export function LaunchForm({
  node,
  config,
  loadScenarios,
  launch,
  onLaunched,
  onCancel,
  hero,
}: {
  node: NodeDefinition | null;
  config: AtlasConfig | null;
  loadScenarios: () => Promise<Scenario[]>;
  launch: LaunchFn;
  onLaunched: (m: Mission) => void;
  onCancel?: () => void;
  hero?: boolean;
}) {
  const liveAvailable = !!config?.live_available;
  const [mode, setMode] = useState<MissionMode>(liveAvailable ? "live" : "simulated");
  const [touched, setTouched] = useState(false);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenarioId, setScenarioId] = useState("");
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [fileErr, setFileErr] = useState<string[]>([]);

  function addFiles(list: File[]) {
    const { accepted, errors } = acceptFiles(list, files);
    setFiles((cur) => [...cur, ...accepted]);
    setFileErr(errors);
  }

  // Follow /config until the user picks a mode themselves.
  useEffect(() => {
    if (!touched) setMode(liveAvailable ? "live" : "simulated");
    else if (!liveAvailable) setMode("simulated");
  }, [liveAvailable, touched]);

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
  const isLive = mode === "live";
  const contextLoaded = !!node && !!config?.context_nodes.includes(node.id);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!node) return;
    const obj = isLive ? objective.trim() : objective.trim() || scenario?.objective || "";
    if (!obj) {
      setErr(isLive ? "Describe the objective for the live agents." : "Describe the objective.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body: LaunchMissionBody = isLive
        ? { objective: obj, node: node.id, mode: "live" }
        : { objective: obj, node: node.id, mode: "simulated", scenario_id: scenarioId || undefined };
      const m = await launch(body, files.length ? files : undefined);
      setObjective("");
      setFiles([]);
      setFileErr([]);
      onLaunched(m);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Launch failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="label">Mode</span>
        <ModeSwitch
          mode={mode}
          liveAvailable={liveAvailable}
          onMode={(m) => {
            setTouched(true);
            setMode(m);
            setErr(null);
          }}
        />
      </div>
      {isLive ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-emerald-500/20 bg-emerald-500/[0.04] px-2.5 py-1.5 font-mono text-[9.5px] tracking-[0.06em] text-dim">
          <span className={contextLoaded ? "text-emerald-300/90" : "text-mute"}>
            {contextLoaded ? "● Node context loaded" : "○ No local context for this node"}
          </span>
          <span>
            orchestrator <span className="text-slate-300">{shortModel(config?.models.orchestrator)}</span>
          </span>
          <span>
            agents <span className="text-slate-300">{shortModel(config?.models.default)}</span>
          </span>
          {config?.web_search && <span className="text-sky-300/80">web search on</span>}
          {config?.backend && <BackendBadge backend={config.backend} costBasis={config.cost_basis} />}
        </div>
      ) : (
        !liveAvailable && (
          <p className="flex items-center gap-1.5 font-mono text-[9.5px] tracking-[0.06em] text-mute">
            <span className="text-amber-300/70">Live agents off</span>
            <span>·</span>
            <span>
              {config?.live_hint ?? (
                <>
                  Add <code className="text-slate-400">ANTHROPIC_API_KEY</code> to <code className="text-slate-400">.env</code> to enable live agents
                </>
              )}
            </span>
          </p>
        )
      )}
      <label className="flex flex-col gap-1.5">
        <span className="label">Objective</span>
        <textarea
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          rows={isLive ? (hero ? 5 : 4) : hero ? 3 : 2}
          placeholder={
            isLive
              ? "Describe what the team should achieve — context, constraints, what a good answer looks like…"
              : scenario?.objective ?? "What should the organization achieve?"
          }
          className="resize-y rounded-md border border-edge bg-black/30 px-3 py-2 text-[13px] leading-relaxed text-ink placeholder:text-mute focus:border-signal/60 focus:outline-none focus:ring-1 focus:ring-signal/30"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e as unknown as React.FormEvent);
          }}
        />
      </label>
      <div className="flex flex-col gap-1.5">
        <span className="label flex items-center justify-between">
          <span>Attachments</span>
          <span className="normal-case tracking-[0.04em] text-mute">agents can read these files</span>
        </span>
        <DropZone onFiles={addFiles} hint="up to 20 · 25 MB each" />
        <PendingFiles files={files} onRemove={(i) => setFiles((cur) => cur.filter((_, j) => j !== i))} />
        {fileErr.map((m) => (
          <p key={m} className="font-mono text-[10px] text-amber-300/90">
            {m}
          </p>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        {isLive ? (
          <p className="min-w-0 flex-1 self-center font-mono text-[9.5px] tracking-[0.06em] text-mute">
            Runs on the Claude API · {node?.name ?? "—"} node · <span className="text-dim">Ctrl+Enter to launch</span>
          </p>
        ) : (
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
        )}
        {onCancel && (
          <button type="button" onClick={onCancel} className="h-9 rounded-md px-3 font-mono text-[11px] uppercase tracking-[0.18em] text-dim hover:text-slate-200">
            Close
          </button>
        )}
        <button
          type="submit"
          disabled={busy || !node}
          className={cx(
            "group flex h-9 items-center gap-2 rounded-md border px-4 font-mono text-[11px] font-semibold uppercase tracking-[0.22em] transition disabled:opacity-50",
            isLive
              ? "border-emerald-400/50 bg-emerald-500/10 text-emerald-300 shadow-[0_0_24px_-8px_#34d399] hover:bg-emerald-500/20"
              : "border-signal/50 bg-signal/10 text-signal shadow-[0_0_24px_-8px_#7dd3fc] hover:bg-signal/20",
          )}
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
            <path d="M3 8h9M8.5 4l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {busy ? "Launching" : isLive ? "Launch live" : "Launch simulation"}
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
  config,
  cancelMission,
  resumeMission,
  agents,
  historyCount,
  onOpenHistory,
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
  launch: LaunchFn;
  onLaunched: (m: Mission) => void;
  config: AtlasConfig | null;
  cancelMission: (id: string) => Promise<Mission>;
  /** POST /missions/{id}/resume */
  resumeMission: (id: string) => Promise<Mission>;
  agents: Map<string, AgentDefinition>;
  /** null = no history endpoint (fallback to the in-state mission picker). */
  historyCount: number | null;
  onOpenHistory: () => void;
}) {
  const [composing, setComposing] = useState(false);
  const [byAgentOpen, setByAgentOpen] = useState(false);
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
            <LaunchForm hero node={node} config={config} loadScenarios={loadScenarios} launch={launch} onLaunched={onLaunched} />
          </div>
        </div>
      </section>
    );
  }

  const done = tasks.filter((t) => t.status === "COMPLETED").length;
  const closed = mission.phase === "CLOSED";
  const interrupted = !!mission.interrupted && !closed;
  const round = mission.round ?? 1;
  const pr = PRIORITY[mission.priority];
  const unfinished = tasks.filter((t) => t.status === "FAILED" || t.status === "CANCELLED").length;
  // Resume: a live mission that stopped (closed or interrupted) with tasks that didn't finish.
  const resumable = mission.mode === "live" && ((closed && unfinished > 0) || !!mission.interrupted);
  const agentUsage = Object.values(mission.usage_by_agent ?? {}).filter((u) => u.llm_calls > 0).length;

  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-col gap-5 px-5 pt-4 pb-5 xl:flex-row xl:items-start">
        {/* objective */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <ModeBadge mode={mission.mode} closed={closed} />
            <span className="label !text-signal/70">{closed || interrupted ? "Mission" : "Active mission"}</span>
            <span className="font-mono text-[10px] text-mute">{mission.id}</span>
            <Tag color={node?.color ?? "#7dd3fc"}>{node?.name ?? mission.node}</Tag>
            <Tag color={pr.color}>{pr.label}</Tag>
            {round > 1 && <Tag color="#c4b5fd">Round {round}</Tag>}
            {interrupted ? (
              <span title="The server stopped while this mission was running. Resume it, or send a message in the thread.">
                <Tag color="#f87171">Interrupted</Tag>
              </span>
            ) : closed ? (
              <Tag color="#cbd5e1">Closed</Tag>
            ) : (
              <Tag color="#22c55e">In flight</Tag>
            )}
            {historyCount !== null && (
              <button
                onClick={onOpenHistory}
                className="ml-1 flex h-[19px] items-center gap-1.5 rounded border border-edge-2 px-1.5 font-mono text-[9.5px] uppercase tracking-[0.16em] text-dim transition hover:border-signal/40 hover:text-slate-200"
                title="Mission history"
              >
                <HistoryIcon />
                History
                {historyCount > 0 && <span className="tracking-normal text-slate-400">· {historyCount}</span>}
              </button>
            )}
            {historyCount === null && missions.length > 1 && (
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
          <h1 className="mt-2 line-clamp-2 text-[19px] leading-snug font-normal tracking-tight text-ink" title={mission.objective}>
            {mission.objective}
          </h1>
          <UsageReadout
            usage={mission.usage}
            mode={mission.mode}
            byAgent={agentUsage > 0 ? { open: byAgentOpen, count: agentUsage, toggle: () => setByAgentOpen((v) => !v) } : undefined}
          />
          {byAgentOpen && agentUsage > 0 && <UsageByAgent byAgent={mission.usage_by_agent} agents={agents} className="mt-2.5 max-w-[640px]" />}
        </div>

        {/* stats + action */}
        <div className="flex shrink-0 items-stretch gap-2">
          <Stat
            label="Elapsed"
            value={interrupted ? "—" : now ? elapsed(mission.created_at, closed && mission.closed_at ? new Date(mission.closed_at).getTime() : now) : "--:--"}
          />
          <Stat label="Tasks" value={`${done}/${tasks.length}`} />
          <Stat label="Agents active" value={`${activeAgents}/${totalAgents}`} />
          <Stat label="Approvals" value={String(pendingApprovals)} alert={pendingApprovals > 0} />
          {!closed && !interrupted && <CancelMission key={mission.id} onConfirm={() => cancelMission(mission.id)} />}
          {resumable && <ResumeMission key={`resume-${mission.id}-${mission.round}`} unfinished={unfinished} onResume={() => resumeMission(mission.id)} />}

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
            config={config}
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
        <PhaseRail phase={mission.phase} dim={interrupted} />
      </div>
    </section>
  );
}

function HistoryIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9M2.5 2.5v2.6h2.6" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8 5v3.2l2.2 1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ModeBadge({ mode, closed }: { mode: Mission["mode"] | undefined; closed: boolean }) {
  if (mode === "live") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded border border-emerald-400/50 bg-emerald-500/15 px-1.5 py-[1px] font-mono text-[9.5px] font-semibold tracking-[0.22em] text-emerald-300">
        <span className={cx("h-1.5 w-1.5 rounded-full bg-emerald-400", !closed && "dot-live")} style={{ ["--c" as string]: "#34d399aa" }} />
        LIVE
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded border border-slate-500/40 bg-slate-500/10 px-1.5 py-[1px] font-mono text-[9.5px] font-semibold tracking-[0.22em] text-slate-400" title="Simulated scenario — no LLM calls">
      SIM
    </span>
  );
}

function UsageReadout({
  usage,
  mode,
  byAgent,
}: {
  usage: Usage | undefined;
  mode: Mission["mode"] | undefined;
  /** toggle for the per-agent breakdown (only when the mission has per-agent usage) */
  byAgent?: { open: boolean; count: number; toggle: () => void };
}) {
  const u = usage ?? { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 };
  if (mode !== "live" && u.llm_calls === 0) {
    return <p className="mt-2 font-mono text-[9.5px] tracking-[0.08em] text-mute">Simulated scenario · no LLM usage</p>;
  }
  const item = (label: string, value: string, title?: string) => (
    <span className="flex items-baseline gap-1.5" title={title}>
      <span className="text-[8.5px] uppercase tracking-[0.2em] text-mute">{label}</span>
      <span className="tabular-nums text-slate-300">{value}</span>
    </span>
  );
  return (
    <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px]" aria-label="LLM usage">
      {item("In", fmtTokens(u.input_tokens), `${u.input_tokens.toLocaleString()} input tokens`)}
      {item("Out", fmtTokens(u.output_tokens), `${u.output_tokens.toLocaleString()} output tokens`)}
      {u.cache_read_tokens > 0 && item("Cached", fmtTokens(u.cache_read_tokens), `${u.cache_read_tokens.toLocaleString()} cache-read tokens`)}
      {item("Calls", String(u.llm_calls))}
      <span className="flex items-baseline gap-1.5" title="Estimated from the configured price table (ATLAS_PRICES)">
        <span className="text-[8.5px] uppercase tracking-[0.2em] text-mute">Cost</span>
        <span className="tabular-nums text-emerald-300">{fmtUsd(u.est_cost_usd)}</span>
        <span className="text-[8.5px] uppercase tracking-[0.16em] text-mute">est.</span>
      </span>
      {byAgent && (
        <button
          onClick={byAgent.toggle}
          aria-expanded={byAgent.open}
          className="flex items-center gap-1 rounded border border-edge-2 px-1.5 text-[8.5px] uppercase tracking-[0.18em] text-dim transition hover:border-signal/40 hover:text-slate-200"
          title="LLM usage by agent for this mission"
        >
          By agent <span className="tracking-normal text-slate-400">· {byAgent.count}</span>
          <span className="text-[9px]">{byAgent.open ? "▴" : "▾"}</span>
        </button>
      )}
    </div>
  );
}

function ResumeMission({ unfinished, onResume }: { unfinished: number; onResume: () => Promise<unknown> }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setErr(null);
    try {
      await onResume();
    } catch (x) {
      setErr(apiErrorText(x, "Resume failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ml-1 flex max-w-[220px] flex-col justify-center gap-1">
      <button
        onClick={go}
        disabled={busy}
        title="Re-run the failed and cancelled tasks, then write a new report version"
        className="flex min-h-9 flex-1 items-center gap-2 rounded-md border border-emerald-400/50 bg-emerald-500/10 px-3 font-mono text-[10.5px] font-semibold uppercase tracking-[0.2em] text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-50"
      >
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
          <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v2.6h-2.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {busy ? "Resuming…" : "Resume"}
        {unfinished > 0 && !busy && <span className="font-normal tracking-normal text-emerald-200/70">· {unfinished}</span>}
      </button>
      {err && (
        <p className="font-mono text-[9.5px] leading-snug text-red-300/90" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}

function CancelMission({ onConfirm }: { onConfirm: () => Promise<unknown> }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setErr(null);
    try {
      await onConfirm();
      setConfirming(false);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Cancel failed");
    } finally {
      setBusy(false);
    }
  }

  if (!confirming) {
    return (
      <button
        onClick={() => setConfirming(true)}
        className="ml-1 rounded-md border border-edge-2 px-3 font-mono text-[10.5px] font-semibold uppercase tracking-[0.2em] text-dim transition hover:border-red-400/50 hover:text-red-300"
      >
        Cancel
      </button>
    );
  }
  return (
    <div className="ml-1 flex flex-col justify-center gap-1 rounded-md border border-red-400/50 bg-red-500/10 px-2.5 py-1" role="group" aria-label="Confirm cancel">
      <span className="font-mono text-[8.5px] uppercase tracking-[0.2em] text-red-300/90">{err ? <span title={err}>Cancel failed</span> : "Stop this mission?"}</span>
      <div className="flex gap-1.5">
        <button
          onClick={go}
          disabled={busy}
          autoFocus
          className="rounded border border-red-400/60 bg-red-500/20 px-2 py-0.5 font-mono text-[9.5px] font-semibold uppercase tracking-[0.16em] text-red-200 hover:bg-red-500/30 disabled:opacity-50"
        >
          {busy ? "Stopping…" : "Stop"}
        </button>
        <button
          onClick={() => {
            setConfirming(false);
            setErr(null);
          }}
          disabled={busy}
          className="rounded px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.16em] text-dim hover:text-slate-200"
        >
          Keep
        </button>
      </div>
    </div>
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
