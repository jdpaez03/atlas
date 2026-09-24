"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { AgentDefinition, AgentReport, Claim, ClaimKind, Confidence, MissionReport, Task } from "@/lib/contracts";
import { CLAIM, cx, hms } from "@/lib/ui";
import { Empty, Panel } from "./primitives";

const KINDS: ClaimKind[] = ["FACT", "SCENARIO", "ASSUMPTION", "RECOMMENDATION"];

function ConfidenceMeter({ c }: { c: Confidence }) {
  const n = c === "HIGH" ? 3 : c === "MEDIUM" ? 2 : 1;
  const color = c === "HIGH" ? "#22c55e" : c === "MEDIUM" ? "#eab308" : "#f97316";
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[8.5px] uppercase tracking-[0.14em] text-dim" title={`Confidence: ${c}`}>
      <span className="flex items-end gap-[2px]">
        {[1, 2, 3].map((i) => (
          <span key={i} className="w-[3px] rounded-sm" style={{ height: 3 + i * 2.5, background: i <= n ? color : "#26324f" }} />
        ))}
      </span>
      {c}
    </span>
  );
}

function ClaimIcon({ kind, color }: { kind: ClaimKind; color: string }) {
  const p = { width: 11, height: 11, viewBox: "0 0 16 16", fill: "none", stroke: color, strokeWidth: 1.6 } as const;
  switch (kind) {
    case "FACT":
      return (
        <svg {...p} aria-hidden>
          <rect x="3" y="3" width="10" height="10" rx="1.5" fill={`${color}33`} />
        </svg>
      );
    case "ASSUMPTION":
      return (
        <svg {...p} aria-hidden>
          <circle cx="8" cy="8" r="5.5" strokeDasharray="2 2" />
        </svg>
      );
    case "SCENARIO":
      return (
        <svg {...p} aria-hidden>
          <path d="M3 13V8m0 0l5-5m-5 5l10 0M8 3l5 5" strokeLinecap="round" />
        </svg>
      );
    case "RECOMMENDATION":
      return (
        <svg {...p} aria-hidden>
          <path d="M3 8h9M8.5 4l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
  }
}

function ClaimItem({ c }: { c: Claim }) {
  const k = CLAIM[c.kind];
  const style =
    c.kind === "ASSUMPTION"
      ? { borderStyle: "dashed", borderColor: `${k.color}66`, background: `${k.color}08` }
      : c.kind === "RECOMMENDATION"
        ? { borderColor: `${k.color}66`, background: `linear-gradient(90deg, ${k.color}1a, transparent)` }
        : { borderColor: `${k.color}33`, borderLeftColor: k.color, background: `${k.color}0a` };
  return (
    <li className={cx("rounded-md border px-3 py-2", c.kind !== "ASSUMPTION" && c.kind !== "RECOMMENDATION" && "border-l-2")} style={style}>
      <p className={cx("text-[12.5px] leading-snug", c.kind === "ASSUMPTION" ? "text-amber-100/80 italic" : c.kind === "RECOMMENDATION" ? "font-medium text-emerald-50" : "text-slate-200")}>
        {c.statement}
      </p>
      <div className="mt-1 flex flex-wrap items-center gap-3">
        <ConfidenceMeter c={c.confidence} />
        {c.sources.length > 0 && <span className="truncate font-mono text-[9.5px] text-mute">src: {c.sources.join(" · ")}</span>}
      </div>
    </li>
  );
}

function Findings({ claims }: { claims: Claim[] }) {
  const groups = KINDS.map((k) => ({ k, items: claims.filter((c) => c.kind === k) })).filter((g) => g.items.length);
  if (!groups.length) return <p className="text-[12px] text-mute">No findings.</p>;
  return (
    <div className="grid gap-4">
      {groups.map(({ k, items }) => (
        <section key={k}>
          <h4 className="mb-1.5 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: CLAIM[k].color }}>
            <ClaimIcon kind={k} color={CLAIM[k].color} />
            {CLAIM[k].label}s <span className="text-mute">· {items.length}</span>
            <span className="ml-1 normal-case tracking-normal text-mute">{CLAIM[k].hint}</span>
          </h4>
          <ul className="grid gap-1.5">
            {items.map((c, i) => (
              <ClaimItem key={i} c={c} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function Block({ title, color, items, icon, empty }: { title: string; color: string; items: string[]; icon: ReactNode; empty?: string }) {
  return (
    <section className="rounded-lg border bg-black/15 p-3" style={{ borderColor: items.length ? `${color}40` : "#1a2340" }}>
      <h4 className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: items.length ? color : "#4a5670" }}>
        {icon}
        {title}
        <span className="text-mute">· {items.length}</span>
      </h4>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-mute">{empty ?? "None."}</p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((t, i) => (
            <li key={i} className="flex gap-2 text-[12px] leading-snug text-slate-300">
              <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full" style={{ background: color }} />
              {t}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const OBJ: Record<MissionReport["objective_status"], { color: string; label: string }> = {
  ACHIEVED: { color: "#22c55e", label: "Objective achieved" },
  PARTIAL: { color: "#eab308", label: "Partially achieved" },
  NOT_ACHIEVED: { color: "#ef4444", label: "Not achieved" },
};

function MissionReportView({ r, agents, reports }: { r: MissionReport; agents: Map<string, AgentDefinition>; reports: AgentReport[] }) {
  const o = OBJ[r.objective_status];
  const contributors = [...new Set(reports.map((x) => x.agent_id))];
  return (
    <div className="grid gap-5 p-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.2em]" style={{ color: o.color, borderColor: `${o.color}55`, background: `${o.color}14` }}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: o.color, boxShadow: `0 0 8px ${o.color}` }} />
            {o.label}
          </span>
          <span className="font-mono text-[10px] text-mute">
            {r.tasks_completed.length} tasks completed · {r.tasks_pending.length} pending · issued {hms(r.created_at)}
          </span>
        </div>
        <h3 className="label mt-4 !text-signal/70">Executive summary</h3>
        <p className="mt-1.5 border-l-2 border-signal/40 pl-4 text-[15px] leading-relaxed font-light text-slate-100">{r.executive_summary}</p>
        {contributors.length > 0 && (
          <p className="mt-2 pl-4 font-mono text-[10px] text-mute">
            Consolidated from{" "}
            {contributors.map((id, i) => (
              <span key={id}>
                {i > 0 && ", "}
                <span style={{ color: agents.get(id)?.color }}>{agents.get(id)?.name ?? id}</span>
              </span>
            ))}
          </p>
        )}
        <h3 className="label mt-5 mb-2 !text-signal/70">Key findings</h3>
        <Findings claims={r.key_findings} />
      </div>
      <div className="grid content-start gap-3">
        <Block
          title="Needs human attention"
          color="#f59e0b"
          items={r.needs_human_attention}
          icon={<span className="text-[11px]">!</span>}
          empty="Nothing requires your decision."
        />
        <Block title="Conflicts" color="#ef4444" items={r.conflicts} icon={<span className="text-[11px]">⇄</span>} empty="No conflicts between agents." />
        <Block title="Assumptions" color="#eab308" items={r.assumptions} icon={<span className="text-[11px]">~</span>} />
        <Block title="Next actions" color="#22c55e" items={r.next_actions} icon={<span className="text-[11px]">→</span>} />
      </div>
    </div>
  );
}

function AgentReportView({ r, agent, task }: { r: AgentReport; agent?: AgentDefinition; task?: Task }) {
  return (
    <div className="grid gap-5 p-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-[15px] font-semibold tracking-[0.2em]" style={{ color: agent?.color }}>
            {agent?.name ?? r.agent_id}
          </span>
          <span className="label !text-[9px]">{agent?.title}</span>
          <ConfidenceMeter c={r.confidence} />
          <span className="font-mono text-[10px] text-mute">{hms(r.created_at)}</span>
        </div>
        {task && <p className="mt-1 font-mono text-[10.5px] text-dim">Task · {task.title}</p>}
        <h3 className="label mt-4 !text-signal/70">Asked to</h3>
        <p className="mt-1 border-l-2 pl-4 text-[14px] leading-relaxed font-light text-slate-100" style={{ borderColor: `${agent?.color ?? "#7dd3fc"}88` }}>
          {r.asked_to}
        </p>
        <h3 className="label mt-5 mb-2 !text-signal/70">Findings</h3>
        <Findings claims={r.findings} />
      </div>
      <div className="grid content-start gap-3">
        <Block title="Actions taken" color="#7dd3fc" items={r.actions_taken} icon={<span className="text-[11px]">✓</span>} />
        <Block title="Inputs used" color="#94a3b8" items={r.inputs_used} icon={<span className="text-[11px]">◇</span>} />
        <Block title="Unresolved" color="#f59e0b" items={r.unresolved} icon={<span className="text-[11px]">?</span>} />
        <Block title="Limitations" color="#f97316" items={r.limitations} icon={<span className="text-[11px]">⚠</span>} />
        {r.needs_agents.length > 0 && <Block title="Needs agents" color="#a78bfa" items={r.needs_agents.map((x) => x.toUpperCase())} icon={<span className="text-[11px]">+</span>} />}
      </div>
    </div>
  );
}

export function Reports({
  missionReport,
  agentReports,
  agents,
  tasks,
  missionActive,
  className,
}: {
  missionReport: MissionReport | undefined;
  agentReports: AgentReport[];
  agents: Map<string, AgentDefinition>;
  tasks: Map<string, Task>;
  missionActive: boolean;
  className?: string;
}) {
  const [tab, setTab] = useState<string>("atlas");
  useEffect(() => {
    if (tab !== "atlas" && !agentReports.some((r) => r.id === tab)) setTab("atlas");
  }, [agentReports, tab]);
  const current = agentReports.find((r) => r.id === tab);

  return (
    <Panel
      code="07"
      title="Reports"
      className={className}
      meta={<span>{agentReports.length} agent reports{missionReport ? " · mission report ready" : ""}</span>}
    >
      <div className="flex gap-1 overflow-x-auto border-b border-edge/60 px-3 pt-2 scroll-thin" role="tablist">
        <TabButton active={tab === "atlas"} onClick={() => setTab("atlas")} color="#e2e8f0" ready={!!missionReport}>
          ATLAS · Mission report
        </TabButton>
        {agentReports.map((r) => {
          const a = agents.get(r.agent_id);
          return (
            <TabButton key={r.id} active={tab === r.id} onClick={() => setTab(r.id)} color={a?.color ?? "#94a3b8"} ready>
              {a?.name ?? r.agent_id}
              {agentReports.filter((x) => x.agent_id === r.agent_id).length > 1 && tasks.get(r.task_id) ? (
                <span className="ml-1 normal-case tracking-normal opacity-60">
                  · {truncate(tasks.get(r.task_id)!.title, 18)}
                </span>
              ) : null}
            </TabButton>
          );
        })}
      </div>
      {tab === "atlas" ? (
        missionReport ? (
          <MissionReportView r={missionReport} agents={agents} reports={agentReports} />
        ) : (
          <Empty className="min-h-40">
            {missionActive
              ? `Mission report pending — ATLAS consolidates once agents deliver. ${agentReports.length} agent report${agentReports.length === 1 ? "" : "s"} in so far.`
              : "Reports appear here once a mission runs."}
          </Empty>
        )
      ) : current ? (
        <AgentReportView r={current} agent={agents.get(current.agent_id)} task={tasks.get(current.task_id)} />
      ) : null}
    </Panel>
  );
}

function TabButton({ active, onClick, color, ready, children }: { active: boolean; onClick: () => void; color: string; ready: boolean; children: ReactNode }) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cx(
        "relative -mb-px flex shrink-0 items-center gap-2 rounded-t-md border border-b-0 px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] transition",
        active ? "border-edge bg-panel-2 text-ink" : "border-transparent text-dim hover:text-slate-200",
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: ready ? color : "#26324f" }} />
      {children}
      {active && <span className="absolute inset-x-2 top-0 h-px" style={{ background: color }} />}
    </button>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
