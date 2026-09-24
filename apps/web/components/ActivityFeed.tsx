"use client";

import type { AgentDefinition, AtlasEvent, EventType } from "@/lib/contracts";
import { hms } from "@/lib/ui";
import { Empty, Panel } from "./primitives";

const KIND: Record<EventType, { tag: string; color: string }> = {
  "mission.created": { tag: "MSN", color: "#7dd3fc" },
  "mission.phase_changed": { tag: "PHS", color: "#7dd3fc" },
  "mission.updated": { tag: "MSN", color: "#475569" },
  "mission.closed": { tag: "MSN", color: "#cbd5e1" },
  "task.created": { tag: "TSK", color: "#94a3b8" },
  "task.updated": { tag: "TSK", color: "#64748b" },
  "agent.state_changed": { tag: "AGT", color: "#64748b" },
  "message.sent": { tag: "MSG", color: "#38bdf8" },
  "report.submitted": { tag: "RPT", color: "#22c55e" },
  "mission.report_ready": { tag: "RPT", color: "#22c55e" },
  "approval.requested": { tag: "HMN", color: "#f59e0b" },
  "approval.decided": { tag: "HMN", color: "#f59e0b" },
  "evidence.recorded": { tag: "EVD", color: "#2dd4bf" },
  "followup.upserted": { tag: "FUP", color: "#f0abfc" },
  "draft.upserted": { tag: "DRF", color: "#34d399" },
  "digest.ready": { tag: "DIG", color: "#c4b5fd" },
  "alert.upserted": { tag: "ALR", color: "#f87171" },
  "rock.updated": { tag: "RCK", color: "#fbbf24" },
  "brief.ready": { tag: "BRF", color: "#fde68a" },
  log: { tag: "LOG", color: "#64748b" },
};

function escape(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight(text: string, names: { re: RegExp | null; color: Map<string, string> }) {
  if (!names.re) return text;
  return text.split(names.re).map((part, i) =>
    names.color.has(part) ? (
      <span key={i} className="font-mono text-[10.5px] font-semibold tracking-[0.06em]" style={{ color: names.color.get(part) }}>
        {part}
      </span>
    ) : (
      part
    ),
  );
}

export function ActivityFeed({ events, agents, className }: { events: AtlasEvent[]; agents: Map<string, AgentDefinition>; className?: string }) {
  const color = new Map([...agents.values()].map((a) => [a.name, a.color]));
  const sorted = [...color.keys()].sort((x, y) => y.length - x.length).map(escape);
  const names = { re: sorted.length ? new RegExp(`(${sorted.join("|")})`, "g") : null, color };
  return (
    <Panel
      code="06"
      title="Activity feed"
      className={className}
      meta={
        <span className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 dot-live" style={{ ["--c" as string]: "#22c55eaa" }} />
          {events.length} events
        </span>
      }
      bodyClassName="overflow-y-auto scroll-thin"
    >
      {events.length === 0 ? (
        <Empty>Event stream is quiet.</Empty>
      ) : (
        <ol className="py-1">
          {events.map((e) => {
            const a = e.agent_id ? agents.get(e.agent_id) : undefined;
            const k = KIND[e.type] ?? KIND.log;
            const important = e.type === "approval.requested" || e.type === "mission.report_ready" || e.type.startsWith("mission.");
            return (
              <li key={e.id} className="feed-in relative grid grid-cols-[56px_30px_minmax(0,1fr)] items-baseline gap-2 px-4 py-[5px]">
                <span className="absolute top-1.5 bottom-1.5 left-0 w-[2px]" style={{ background: a?.color ?? "#26324f", opacity: 0.7 }} />
                <span className="font-mono text-[10px] tabular-nums text-mute">{hms(e.ts)}</span>
                <span className="font-mono text-[8.5px] tracking-[0.12em]" style={{ color: k.color }}>
                  {k.tag}
                </span>
                <span className={important ? "text-[11.5px] leading-snug text-slate-100" : "text-[11.5px] leading-snug text-slate-400"}>
                  {highlight(e.summary, names)}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}
