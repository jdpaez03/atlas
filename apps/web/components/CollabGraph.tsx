"use client";

import type { AgentDefinition, AgentMessage, AgentState } from "@/lib/contracts";
import { MSG, STATUS, aggregateStatus, hms, msgFrom, msgTo, useNow } from "@/lib/ui";
import type { DivisionGroup } from "./AgentBoard";
import { Panel } from "./primitives";

const W = 640;
const H = 340;
const CX = W / 2;
const CY = H / 2 + 4;
const PULSE_MS = 6000;

interface P {
  x: number;
  y: number;
  r: number;
}

function curve(a: P, b: P, bend: number) {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  return { cx: mx - (dy / len) * bend, cy: my + (dx / len) * bend };
}

export function CollabGraph({
  orchestrator,
  core,
  divisions,
  states,
  messages,
  agents,
  className,
}: {
  orchestrator: AgentDefinition | undefined;
  core: AgentDefinition[];
  divisions: DivisionGroup[];
  states: Map<string, AgentState>;
  messages: AgentMessage[];
  agents: Map<string, AgentDefinition>;
  className?: string;
}) {
  const now = useNow(500);
  const pos = new Map<string, P>();
  if (orchestrator) pos.set(orchestrator.id, { x: CX, y: CY, r: 30 });

  const slots: ({ kind: "agent"; a: AgentDefinition } | { kind: "div"; g: DivisionGroup })[] = [
    ...core.map((a) => ({ kind: "agent" as const, a })),
    ...divisions.map((g) => ({ kind: "div" as const, g })),
  ];
  const RX = 232;
  const RY = 118;
  const hubs: { g: DivisionGroup; p: P; angle: number }[] = [];
  slots.forEach((slot, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, slots.length);
    const p = { x: CX + RX * Math.cos(angle), y: CY + RY * Math.sin(angle) };
    if (slot.kind === "agent") pos.set(slot.a.id, { ...p, r: 21 });
    else {
      const hub = { ...p, r: 17 };
      hubs.push({ g: slot.g, p: hub, angle });
      const n = slot.g.members.length;
      const span = Math.PI * 1.25;
      // satellites on an arc facing away from the centre
      const out = Math.atan2(p.y - CY, (p.x - CX) * (RY / RX));
      slot.g.members.forEach((m, k) => {
        const t = out - span / 2 + (n <= 1 ? span / 2 : (k * span) / (n - 1));
        pos.set(m.id, { x: p.x + 42 * Math.cos(t), y: p.y + 42 * Math.sin(t), r: 7.5 });
      });
    }
  });

  // edges aggregated by unordered pair
  type Edge = { key: string; a: string; b: string; count: number; last: AgentMessage };
  const edges = new Map<string, Edge>();
  for (const m of messages) {
    const f = msgFrom(m);
    const t = msgTo(m);
    if (!pos.has(f) || !pos.has(t) || f === t) continue;
    const [a, b] = f < t ? [f, t] : [t, f];
    const key = `${a}|${b}`;
    const e = edges.get(key);
    if (!e) edges.set(key, { key, a, b, count: 1, last: m });
    else {
      e.count++;
      if (m.created_at >= e.last.created_at) e.last = m;
    }
  }
  const recent = now ? messages.filter((m) => now - new Date(m.created_at).getTime() < PULSE_MS) : [];
  const last = messages[messages.length - 1];
  const statusOf = (id: string) => states.get(id)?.status ?? "IDLE";

  const edgeGeom = (a: string, b: string) => {
    const pa = pos.get(a)!;
    const pb = pos.get(b)!;
    return { pa, pb, ...curve(pa, pb, 16) };
  };

  return (
    <Panel
      code="03"
      title="Collaboration graph"
      className={className}
      meta={
        <span>
          <span className="text-slate-300">{messages.length}</span> messages · <span className="text-slate-300">{edges.size}</span> links
        </span>
      }
      bodyClassName="relative flex flex-col"
    >
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label="Agent collaboration graph">
        <defs>
          <radialGradient id="cg-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#7dd3fc" stopOpacity="0.16" />
            <stop offset="100%" stopColor="#7dd3fc" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* orbit guides */}
        <ellipse cx={CX} cy={CY} rx={RX} ry={RY} fill="none" stroke="#1c2540" strokeDasharray="2 6" />
        <ellipse cx={CX} cy={CY} rx={RX * 0.55} ry={RY * 0.55} fill="none" stroke="#141c33" />
        <circle cx={CX} cy={CY} r={110} fill="url(#cg-core)" />

        {/* organizational spokes */}
        {orchestrator &&
          [...core.map((a) => pos.get(a.id)!), ...hubs.map((h) => h.p)].map((p, i) => (
            <line key={i} x1={CX} y1={CY} x2={p.x} y2={p.y} stroke="#1c2540" strokeWidth={1} />
          ))}
        {hubs.map(({ g, p }) =>
          g.members.map((m) => {
            const s = pos.get(m.id)!;
            return <line key={m.id} x1={p.x} y1={p.y} x2={s.x} y2={s.y} stroke={g.division.color} strokeOpacity={0.25} />;
          }),
        )}

        {/* message edges */}
        {[...edges.values()].map((e) => {
          const { pa, pb, cx, cy } = edgeGeom(e.a, e.b);
          const color = MSG[e.last.type]?.color ?? "#7dd3fc";
          const hot = now && now - new Date(e.last.created_at).getTime() < PULSE_MS;
          return (
            <g key={e.key}>
              <path d={`M${pa.x},${pa.y} Q${cx},${cy} ${pb.x},${pb.y}`} fill="none" stroke={color} strokeOpacity={hot ? 0.9 : 0.4} strokeWidth={1 + Math.min(e.count, 6) * 0.45} />
              {hot && (
                <path
                  d={`M${pa.x},${pa.y} Q${cx},${cy} ${pb.x},${pb.y}`}
                  fill="none"
                  stroke={color}
                  strokeWidth={3}
                  strokeOpacity={0.35}
                  className="edge-flow"
                  style={{ filter: `drop-shadow(0 0 4px ${color})` }}
                />
              )}
            </g>
          );
        })}

        {/* pulses travelling from sender to receiver */}
        {recent.map((m) => {
          const f = msgFrom(m);
          const t = msgTo(m);
          if (!pos.has(f) || !pos.has(t) || f === t) return null;
          const [a, b] = f < t ? [f, t] : [t, f];
          const { cx, cy } = edgeGeom(a, b);
          const pf = pos.get(f)!;
          const pt = pos.get(t)!;
          const color = MSG[m.type]?.color ?? "#7dd3fc";
          return (
            <circle key={m.id} r={3.2} fill={color} style={{ filter: `drop-shadow(0 0 5px ${color})` }}>
              <animateMotion dur="1.3s" repeatCount="indefinite" path={`M${pf.x},${pf.y} Q${cx},${cy} ${pt.x},${pt.y}`} />
            </circle>
          );
        })}

        {/* division hubs */}
        {hubs.map(({ g, p }) => {
          const agg = aggregateStatus(g.members.map((m) => statusOf(m.id)));
          const s = STATUS[agg];
          const hex = Array.from({ length: 6 }, (_, k) => {
            const t = Math.PI / 6 + (k * Math.PI) / 3;
            return `${p.x + p.r * Math.cos(t)},${p.y + p.r * Math.sin(t)}`;
          }).join(" ");
          return (
            <g key={g.division.id}>
              <polygon points={hex} fill="#0a0f1c" stroke={s.active ? s.color : g.division.color} strokeOpacity={s.active ? 1 : 0.6} strokeWidth={1.5} style={s.active ? { filter: `drop-shadow(0 0 6px ${s.color})` } : undefined} />
              <text x={p.x} y={p.y + 3} textAnchor="middle" className="font-mono" fontSize={8.5} fontWeight={600} fill={g.division.color} letterSpacing="0.12em">
                {g.division.name}
              </text>
            </g>
          );
        })}

        {/* agents */}
        {[...pos.entries()].map(([id, p]) => {
          const a = agents.get(id);
          if (!a) return null;
          const status = statusOf(id);
          const s = STATUS[status];
          const isCenter = a.is_orchestrator;
          const sat = p.r < 10;
          const label = sat ? a.name.replace(/^.*·/, "").slice(0, 4) : a.name;
          const below = p.y >= CY - 10 || isCenter;
          return (
            <g key={id}>
              {isCenter && (
                <>
                  <circle cx={p.x} cy={p.y} r={p.r + 9} fill="none" stroke="#7dd3fc" strokeOpacity={0.35} strokeDasharray="3 5" className="spin-slow" />
                  <circle cx={p.x} cy={p.y} r={p.r + 16} fill="none" stroke="#7dd3fc" strokeOpacity={0.12} strokeDasharray="1 7" className="spin-rev" />
                </>
              )}
              <circle
                cx={p.x}
                cy={p.y}
                r={p.r}
                fill="#0a0f1c"
                stroke={s.active ? s.color : `${a.color}88`}
                strokeWidth={sat ? 1.5 : 2}
                style={s.active ? { filter: `drop-shadow(0 0 ${sat ? 4 : 8}px ${s.color})` } : undefined}
              />
              {!sat && <circle cx={p.x} cy={p.y} r={p.r - 5} fill={a.color} fillOpacity={0.08} />}
              {!sat && (
                <text x={p.x} y={p.y + 3.5} textAnchor="middle" className="font-mono" fontSize={isCenter ? 10 : 9} fontWeight={700} fill={a.color} letterSpacing="0.1em">
                  {isCenter ? "ATLAS" : a.name.slice(0, 3)}
                </text>
              )}
              {sat && <circle cx={p.x} cy={p.y} r={2.5} fill={s.active ? s.color : a.color} fillOpacity={s.active ? 1 : 0.5} />}
              {!isCenter && (
                <text
                  x={p.x}
                  y={below ? p.y + p.r + (sat ? 10 : 13) : p.y - p.r - (sat ? 5 : 7)}
                  textAnchor="middle"
                  className="font-mono"
                  fontSize={sat ? 7 : 8.5}
                  fill={sat ? "#7b879f" : "#c8d2e3"}
                  letterSpacing="0.14em"
                >
                  {label}
                </text>
              )}
              {!sat && !isCenter && status !== "IDLE" && (
                <text x={p.x} y={below ? p.y + p.r + 23 : p.y - p.r - 17} textAnchor="middle" className="font-mono" fontSize={7} fill={s.color} letterSpacing="0.16em">
                  {s.label.toUpperCase()}
                </text>
              )}
              {isCenter && (
                <text x={p.x} y={p.y + p.r + 30} textAnchor="middle" className="font-mono" fontSize={7} fill={s.color} letterSpacing="0.16em">
                  {s.label.toUpperCase()}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div className="mt-auto flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 border-t border-edge/60 px-4 py-2">
        <div className="min-w-0 flex-1 truncate font-mono text-[10px] text-dim">
          {last ? (
            <>
              <span className="text-mute">{hms(last.created_at)}</span>{" "}
              <span style={{ color: agents.get(msgFrom(last))?.color }}>{agents.get(msgFrom(last))?.name ?? msgFrom(last)}</span>
              <span className="text-mute"> → </span>
              <span style={{ color: agents.get(msgTo(last))?.color }}>{agents.get(msgTo(last))?.name ?? msgTo(last)}</span>{" "}
              <span style={{ color: MSG[last.type]?.color }}>{last.type}</span> <span className="text-slate-400">{last.subject}</span>
            </>
          ) : (
            <span className="text-mute">No inter-agent traffic yet</span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          {Object.entries(MSG).map(([k, v]) => (
            <span key={k} className="flex items-center gap-1 font-mono text-[8.5px] uppercase tracking-[0.12em] text-dim">
              <span className="h-[2px] w-2.5 rounded" style={{ background: v.color }} />
              {v.label}
            </span>
          ))}
        </div>
      </div>
    </Panel>
  );
}
