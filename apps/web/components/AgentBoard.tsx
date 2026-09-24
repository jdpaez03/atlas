"use client";

import { useState } from "react";
import type { AgentDefinition, AgentState, AgentStatus, DivisionDefinition, Task } from "@/lib/contracts";
import { STATUS, aggregateStatus, cx } from "@/lib/ui";
import { Emblem, Panel, ProgressBar, StatusDot, StatusPill } from "./primitives";

export interface DivisionGroup {
  division: DivisionDefinition;
  members: AgentDefinition[];
}

type Lookup = {
  states: Map<string, AgentState>;
  tasks: Map<string, Task>;
  agents: Map<string, AgentDefinition>;
};

const statusOf = (l: Lookup, id: string): AgentStatus => l.states.get(id)?.status ?? "IDLE";

function glowStyle(status: AgentStatus) {
  const s = STATUS[status];
  return s.active ? { ["--g" as string]: `${s.color}55`, borderColor: `${s.color}66` } : undefined;
}

function CurrentTask({ task, color }: { task: Task | undefined; color: string }) {
  if (!task) return null;
  return (
    <div className="mt-2.5">
      <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
        <span className="truncate text-slate-400">
          <span className="text-mute">▸ </span>
          {task.title}
        </span>
        <span className="shrink-0 tabular-nums text-dim">{Math.round((task.progress > 1 ? task.progress / 100 : task.progress) * 100)}%</span>
      </div>
      <ProgressBar value={task.progress} color={color} active={task.status === "IN_PROGRESS"} className="mt-1" />
    </div>
  );
}

function OrchestratorCard({ agent, l }: { agent: AgentDefinition; l: Lookup }) {
  const st = l.states.get(agent.id);
  const status = st?.status ?? "IDLE";
  const s = STATUS[status];
  const task = st?.current_task_id ? l.tasks.get(st.current_task_id) : undefined;
  return (
    <article
      className={cx("relative overflow-hidden rounded-lg border border-edge-2 bg-gradient-to-br from-white/[0.04] to-transparent p-4", s.active && "glow")}
      style={glowStyle(status)}
    >
      <div className="pointer-events-none absolute -top-16 -right-10 h-40 w-40 rounded-full opacity-30 blur-3xl" style={{ background: s.active ? s.color : "#7dd3fc" }} />
      <div className="relative flex items-start gap-4">
        <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full border border-edge-2 bg-black/40">
          <Emblem size={40} color={agent.color} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <h3 className="font-mono text-[20px] font-semibold tracking-[0.34em] text-ink">{agent.name}</h3>
            <StatusPill status={status} />
          </div>
          <p className="label mt-0.5 !text-[9.5px]">{agent.title}</p>
          <p className={cx("mt-2 line-clamp-2 text-[12.5px] leading-snug", st?.activity ? "text-slate-200" : "text-mute")}>
            {st?.activity ?? "Awaiting objective — ready to decompose, delegate and consolidate."}
          </p>
          {st && st.collaborating_with.length > 0 && (
            <p className="mt-1 font-mono text-[10px] text-blue-300/80">⇄ {st.collaborating_with.map((id) => l.agents.get(id)?.name ?? id).join(", ")}</p>
          )}
          <CurrentTask task={task} color={s.color} />
        </div>
      </div>
    </article>
  );
}

function AgentCard({ agent, l }: { agent: AgentDefinition; l: Lookup }) {
  const st = l.states.get(agent.id);
  const status = st?.status ?? "IDLE";
  const s = STATUS[status];
  const task = st?.current_task_id ? l.tasks.get(st.current_task_id) : undefined;
  return (
    <article
      className={cx(
        "relative flex min-w-0 flex-col rounded-lg border border-edge bg-white/[0.02] p-3 transition-colors",
        s.active && "glow",
        status === "BLOCKED" && "bg-red-500/[0.05]",
      )}
      style={glowStyle(status)}
      title={agent.description}
    >
      <span className="absolute top-3 bottom-3 left-0 w-[2px] rounded-full" style={{ background: agent.color, opacity: s.active ? 0.9 : 0.35 }} />
      <div className="flex items-center justify-between gap-2 pl-1.5">
        <h3 className="truncate font-mono text-[13px] font-semibold tracking-[0.2em]" style={{ color: agent.color }}>
          {agent.name}
        </h3>
        <StatusDot status={status} size={7} />
      </div>
      <p className="mt-0.5 flex min-w-0 items-center gap-1.5 pl-1.5 font-mono text-[9px] uppercase tracking-[0.14em]">
        <span className="shrink-0 font-semibold" style={{ color: s.color }}>
          {s.label}
        </span>
        <span className="text-mute">·</span>
        <span className="truncate text-dim">{agent.title}</span>
      </p>
      <p className={cx("mt-2 line-clamp-2 min-h-[2.6em] pl-1.5 text-[11.5px] leading-[1.3]", st?.activity ? "text-slate-300" : "text-mute")}>
        {st?.activity ?? "Standing by"}
      </p>
      <div className="pl-1.5">
        <CurrentTask task={task} color={s.color} />
      </div>
    </article>
  );
}

function DivisionCard({ group, l }: { group: DivisionGroup; l: Lookup }) {
  const [open, setOpen] = useState(false);
  const { division, members } = group;
  const statuses = members.map((m) => statusOf(l, m.id));
  const agg = aggregateStatus(statuses);
  const active = statuses.filter((s) => STATUS[s].active).length;
  const s = STATUS[agg];
  return (
    <article className={cx("rounded-lg border border-edge bg-white/[0.02]", STATUS[agg].active && "glow")} style={glowStyle(agg)}>
      <button onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-3 p-3 text-left" aria-expanded={open}>
        <svg width="30" height="30" viewBox="0 0 32 32" fill="none" aria-hidden className="shrink-0">
          <path d="M16 2.5l11.7 6.75v13.5L16 29.5 4.3 22.75V9.25z" stroke={division.color} strokeOpacity="0.8" />
          <path d="M16 9l6 3.5v7L16 23l-6-3.5v-7z" fill={division.color} fillOpacity="0.18" stroke={division.color} strokeOpacity="0.6" />
        </svg>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="font-mono text-[13px] font-semibold tracking-[0.24em]" style={{ color: division.color }}>
              {division.name}
            </h3>
            <span className="label !text-[9px]">Division</span>
          </div>
          <p className="mt-0.5 truncate font-mono text-[10px] text-dim">
            {members.length} specialists · <span style={{ color: active ? s.color : undefined }}>{active} active</span>
          </p>
        </div>
        <StatusPill status={agg} compact />
        <svg width="12" height="12" viewBox="0 0 16 16" className={cx("shrink-0 text-dim transition-transform", open && "rotate-180")} aria-hidden>
          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </button>
      {!open && (
        <div className="grid grid-cols-6 gap-1 px-3 pb-3" aria-hidden>
          {members.map((m) => {
            const st = statusOf(l, m.id);
            const on = STATUS[st].active;
            return (
              <div
                key={m.id}
                className="flex items-center justify-center gap-1 rounded border py-1 font-mono text-[8.5px] tracking-[0.1em]"
                style={{ borderColor: on ? `${STATUS[st].color}66` : "#1a2340", color: on ? STATUS[st].color : "#7b879f", background: on ? `${STATUS[st].color}12` : undefined }}
                title={`${m.name} · ${STATUS[st].label}`}
              >
                <StatusDot status={st} size={5} />
                {m.name.replace(/^.*·/, "").slice(0, 4)}
              </div>
            );
          })}
        </div>
      )}
      {open && (
        <div className="border-t border-edge/70 px-3 pt-2 pb-3">
          <p className="mb-2 text-[11px] leading-snug text-slate-500">{division.description}</p>
          <ul className="grid gap-1.5">
            {members.map((m) => {
              const st = l.states.get(m.id);
              const status = st?.status ?? "IDLE";
              const task = st?.current_task_id ? l.tasks.get(st.current_task_id) : undefined;
              return (
                <li key={m.id} className="rounded-md border border-edge/70 bg-black/20 px-2.5 py-2" title={m.description}>
                  <div className="flex items-center gap-2">
                    <StatusDot status={status} size={6} />
                    <span className="w-[98px] shrink-0 truncate font-mono text-[11px] font-semibold tracking-[0.12em]" style={{ color: m.color }}>
                      {m.name}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">{st?.activity ?? m.title}</span>
                    <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.14em]" style={{ color: STATUS[status].color }}>
                      {STATUS[status].label}
                    </span>
                  </div>
                  {task && <CurrentTask task={task} color={STATUS[status].color} />}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </article>
  );
}

export function AgentBoard({
  orchestrator,
  core,
  divisions,
  states,
  tasks,
  agents,
  className,
}: {
  orchestrator: AgentDefinition | undefined;
  core: AgentDefinition[];
  divisions: DivisionGroup[];
  states: Map<string, AgentState>;
  tasks: Map<string, Task>;
  agents: Map<string, AgentDefinition>;
  className?: string;
}) {
  const l: Lookup = { states, tasks, agents };
  const all = [...(orchestrator ? [orchestrator] : []), ...core, ...divisions.flatMap((d) => d.members)];
  const active = all.filter((a) => STATUS[statusOf(l, a.id)].active).length;
  return (
    <Panel
      code="02"
      title="Agent board"
      className={className}
      meta={
        <span>
          <span className="text-slate-300">{active}</span>/{all.length} active
        </span>
      }
      bodyClassName="flex flex-col gap-3 p-3"
    >
      {orchestrator && <OrchestratorCard agent={orchestrator} l={l} />}
      <div className="relative flex items-center gap-2 px-1">
        <span className="h-px flex-1 bg-gradient-to-r from-transparent to-edge-2" />
        <span className="label !text-[9px]">Core specialists</span>
        <span className="h-px flex-1 bg-gradient-to-l from-transparent to-edge-2" />
      </div>
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {core.map((a) => (
          <AgentCard key={a.id} agent={a} l={l} />
        ))}
      </div>
      {divisions.length > 0 && (
        <>
          <div className="relative flex items-center gap-2 px-1">
            <span className="h-px flex-1 bg-gradient-to-r from-transparent to-edge-2" />
            <span className="label !text-[9px]">Divisions</span>
            <span className="h-px flex-1 bg-gradient-to-l from-transparent to-edge-2" />
          </div>
          {divisions.map((g) => (
            <DivisionCard key={g.division.id} group={g} l={l} />
          ))}
        </>
      )}
    </Panel>
  );
}
