"use client";

import type { AgentDefinition, Task } from "@/lib/contracts";
import { PRIORITY, REASON_LABEL, TASK_STATUS, cx, pct } from "@/lib/ui";
import { AgentName, Empty, Panel, ProgressBar } from "./primitives";

const COLS = "grid-cols-[26px_minmax(0,1fr)_76px_80px_72px] @[600px]:grid-cols-[30px_minmax(0,1fr)_84px_84px_34px_58px_78px]";

export function TaskBoard({ tasks, agents, className }: { tasks: Task[]; agents: Map<string, AgentDefinition>; className?: string }) {
  const index = new Map(tasks.map((t, i) => [t.id, `T${String(i + 1).padStart(2, "0")}`]));
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const done = tasks.filter((t) => t.status === "COMPLETED").length;
  const overall = tasks.length ? tasks.reduce((a, t) => a + pct(t.progress), 0) / tasks.length / 100 : 0;

  return (
    <Panel
      code="04"
      title="Task board"
      className={className}
      meta={
        tasks.length ? (
          <div className="flex items-center gap-2 whitespace-nowrap">
            <span>
              <span className="text-slate-300">{done}</span>/{tasks.length} done
            </span>
            <ProgressBar value={overall} color="#7dd3fc" className="w-20" />
          </div>
        ) : null
      }
      bodyClassName="@container"
    >
      {tasks.length === 0 ? (
        <Empty>No tasks yet — ATLAS will decompose the objective into a task graph.</Empty>
      ) : (
        <div>
          <div className={cx("grid items-center gap-2.5 border-b border-edge/60 px-4 py-2 font-mono text-[9px] uppercase tracking-[0.2em] text-mute", COLS)}>
            <span>ID</span>
            <span>Task</span>
            <span>Agent</span>
            <span>Status</span>
            <span className="hidden @[600px]:block">Pri</span>
            <span className="hidden @[600px]:block">Deps</span>
            <span className="text-right">Progress</span>
          </div>
          <ul>
            {tasks.map((t) => {
              const st = TASK_STATUS[t.status];
              const pr = PRIORITY[t.priority];
              const agent = t.assigned_to ? agents.get(t.assigned_to) : undefined;
              const awaiting = t.status === "AWAITING_APPROVAL";
              const faded = t.status === "COMPLETED" || t.status === "CANCELLED";
              return (
                <li
                  key={t.id}
                  className={cx(
                    "grid items-center gap-2.5 border-b border-edge/40 px-4 py-2 transition-colors last:border-b-0 hover:bg-white/[0.02]",
                    COLS,
                    awaiting && "bg-amber-500/[0.06]",
                  )}
                  title={t.description}
                >
                  <span className="font-mono text-[10px] text-mute">{index.get(t.id)}</span>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={cx("truncate text-[12.5px]", faded ? "text-slate-400" : "text-slate-100")}>{t.title}</span>
                      {t.requires_approval && (
                        <span
                          className={cx(
                            "order-first inline-flex shrink-0 items-center gap-1 rounded border px-[3px] py-[2px] font-mono text-[8.5px] uppercase tracking-[0.14em]",
                            awaiting ? "border-amber-400/60 bg-amber-400/15 text-amber-300" : "border-amber-500/30 text-amber-400/70",
                          )}
                          title={t.approval_reason ? `Human approval: ${REASON_LABEL[t.approval_reason] ?? t.approval_reason}` : "Requires human approval"}
                        >
                          <svg width="9" height="9" viewBox="0 0 16 16" fill="none" aria-hidden>
                            <path d="M8 1.5l5.5 2v4.2c0 3.3-2.3 5.6-5.5 6.8-3.2-1.2-5.5-3.5-5.5-6.8V3.5z" stroke="currentColor" strokeWidth="1.6" />
                          </svg>
                        </span>
                      )}
                    </div>
                    <p className="truncate text-[10.5px] text-mute">{t.description}</p>
                  </div>
                  <AgentName agent={agent} id={t.assigned_to} className="truncate text-[10.5px]" />
                  <span className="inline-flex items-center gap-1.5 whitespace-nowrap font-mono text-[9.5px] uppercase tracking-[0.1em]" style={{ color: st.color }}>
                    <span className={cx("h-1.5 w-1.5 shrink-0 rounded-full", (t.status === "IN_PROGRESS" || awaiting) && "dot-live")} style={{ background: st.color, ["--c" as string]: `${st.color}aa` }} />
                    {st.label}
                  </span>
                  <span className="hidden font-mono text-[9.5px] tracking-[0.1em] @[600px]:block" style={{ color: pr.color }}>
                    {pr.label}
                  </span>
                  <span className="hidden flex-wrap gap-1 font-mono text-[9.5px] @[600px]:flex">
                    {t.depends_on.length === 0 ? (
                      <span className="text-mute">—</span>
                    ) : (
                      [
                      ...t.depends_on.slice(0, t.depends_on.length > 3 ? 2 : 3).map((d) => {
                        const dep = byId.get(d);
                        const ok = dep?.status === "COMPLETED";
                        return (
                          <span key={d} className={ok ? "text-slate-500 line-through decoration-slate-600" : "text-amber-300/80"} title={dep?.title}>
                            {index.get(d) ?? "?"}
                          </span>
                        );
                      }),
                      t.depends_on.length > 3 ? (
                        <span key="more" className="text-dim" title={t.depends_on.map((d) => index.get(d)).join(", ")}>
                          +{t.depends_on.length - 2}
                        </span>
                      ) : null,
                      ]
                    )}
                  </span>
                  <div className="flex items-center gap-2">
                    <ProgressBar value={t.progress} color={st.color} active={t.status === "IN_PROGRESS"} />
                    <span className="w-7 shrink-0 text-right font-mono text-[9.5px] tabular-nums text-dim">{pct(t.progress)}%</span>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </Panel>
  );
}
