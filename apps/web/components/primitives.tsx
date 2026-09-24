"use client";

import type { CSSProperties, ReactNode } from "react";
import type { AgentDefinition, AgentStatus } from "@/lib/contracts";
import { STATUS, cx, pct } from "@/lib/ui";

export function Panel({
  code,
  title,
  meta,
  children,
  className,
  bodyClassName,
  style,
}: {
  code?: string;
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  style?: CSSProperties;
}) {
  return (
    <section className={cx("panel flex min-w-0 flex-col", className)} style={style}>
      <header className="flex items-center justify-between gap-3 border-b border-edge/80 px-4 py-2.5">
        <h2 className="label flex items-center gap-2 whitespace-nowrap !text-slate-300">
          {code && <span className="text-signal/60">{code}</span>}
          <span className="text-signal/30">//</span>
          {title}
        </h2>
        {meta && <div className="flex min-w-0 items-center gap-2 font-mono text-[10.5px] text-dim">{meta}</div>}
      </header>
      <div className={cx("min-h-0 flex-1", bodyClassName)}>{children}</div>
    </section>
  );
}

export function StatusDot({ status, size = 8 }: { status: AgentStatus; size?: number }) {
  const s = STATUS[status] ?? STATUS.IDLE;
  return (
    <span
      className={cx("inline-block shrink-0 rounded-full", s.active && "dot-live")}
      style={{ width: size, height: size, background: s.color, ["--c" as string]: `${s.color}aa`, boxShadow: s.active ? `0 0 8px ${s.color}` : undefined }}
    />
  );
}

export function StatusPill({ status, compact }: { status: AgentStatus; compact?: boolean }) {
  const s = STATUS[status] ?? STATUS.IDLE;
  return (
    <span
      className={cx(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border font-mono uppercase tracking-[0.14em]",
        compact ? "px-1.5 py-[1px] text-[9px]" : "px-2 py-0.5 text-[9.5px]",
      )}
      style={{ color: s.color, borderColor: `${s.color}55`, background: `${s.color}14` }}
    >
      <StatusDot status={status} size={compact ? 5 : 6} />
      {s.label}
    </span>
  );
}

export function AgentName({ agent, id, className }: { agent?: AgentDefinition; id?: string | null; className?: string }) {
  if (!agent && !id) return <span className={cx("font-mono text-mute", className)}>—</span>;
  return (
    <span className={cx("font-mono font-semibold tracking-[0.08em]", className)} style={{ color: agent?.color ?? "#94a3b8" }}>
      {agent?.name ?? id?.toUpperCase()}
    </span>
  );
}

export function ProgressBar({ value, color = "#22c55e", active, className }: { value: number; color?: string; active?: boolean; className?: string }) {
  const v = pct(value);
  return (
    <div className={cx("h-1 w-full overflow-hidden rounded-full bg-white/[0.06]", className)}>
      <div
        className={cx("h-full rounded-full transition-[width] duration-700 ease-out", active && "bar-sweep")}
        style={{ width: `${v}%`, background: `linear-gradient(90deg, ${color}66, ${color})`, boxShadow: v ? `0 0 8px ${color}66` : undefined }}
      />
    </div>
  );
}

export function Tag({ color, children, className }: { color: string; children: ReactNode; className?: string }) {
  return (
    <span
      className={cx("inline-flex items-center rounded border px-1.5 py-[1px] font-mono text-[9.5px] uppercase tracking-[0.14em]", className)}
      style={{ color, borderColor: `${color}44`, background: `${color}12` }}
    >
      {children}
    </span>
  );
}

export function Emblem({ size = 28, color = "#7dd3fc" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-hidden>
      <circle cx="20" cy="20" r="18" stroke={color} strokeOpacity="0.35" strokeDasharray="2 3" className="spin-rev" />
      <circle cx="20" cy="20" r="12.5" stroke={color} strokeOpacity="0.7" />
      <path d="M20 7.5 L30.8 26.25 H9.2 Z" stroke={color} strokeWidth="1.2" strokeLinejoin="round" />
      <circle cx="20" cy="20" r="2.4" fill={color} />
    </svg>
  );
}

export function Empty({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("flex h-full min-h-24 items-center justify-center px-6 py-8 text-center font-mono text-[11px] tracking-wider text-mute", className)}>{children}</div>;
}
