"use client";

import { useState } from "react";
import type { Decision } from "@/lib/api";
import type { AgentDefinition, ApprovalRequest } from "@/lib/contracts";
import { QUESTION_REASONS, cx, hms, reasonLabel, useNow } from "@/lib/ui";
import { AgentName, Panel } from "./primitives";

function PendingCard({
  a,
  agents,
  decide,
}: {
  a: ApprovalRequest;
  agents: Map<string, AgentDefinition>;
  decide: (id: string, d: Decision, note?: string) => Promise<ApprovalRequest>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<Decision | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const now = useNow(1000);
  const waited = now ? Math.max(0, Math.floor((now - new Date(a.created_at).getTime()) / 1000)) : 0;
  const question = QUESTION_REASONS.has(a.reason);
  const waitedLabel = waited >= 3600 ? `${Math.floor(waited / 3600)}h ${Math.floor((waited % 3600) / 60)}m` : waited >= 60 ? `${Math.floor(waited / 60)}m ${waited % 60}s` : `${waited}s`;

  async function go(d: Decision) {
    setBusy(d);
    setErr(null);
    try {
      await decide(a.id, d, note.trim() || undefined);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Decision failed");
      setBusy(null);
    }
  }

  return (
    <article className="alarm relative overflow-hidden rounded-lg border border-amber-500/40 bg-gradient-to-br from-red-500/[0.10] via-amber-500/[0.05] to-transparent p-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 rounded border border-red-400/50 bg-red-500/15 px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.18em] text-red-300">
          <span className="h-1.5 w-1.5 rounded-full bg-red-400 dot-live" style={{ ["--c" as string]: "#ef4444aa" }} />
          {question ? "Agent is asking" : "Decision required"}
        </span>
        <span className="font-mono text-[9.5px] tabular-nums text-amber-300/80">waiting {waitedLabel}</span>
      </div>
      <h3 className="mt-2.5 text-[14px] leading-snug font-medium text-ink">{a.title}</h3>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 font-mono text-[9.5px] uppercase tracking-[0.12em] text-amber-300/80">
        <span
          className={cx("rounded border px-1 py-[0.5px]", question ? "border-sky-400/40 bg-sky-500/10 text-sky-300" : "border-amber-400/40 bg-amber-500/10")}
          title={a.reason}
        >
          {reasonLabel(a.reason)}
        </span>
        <span className="text-mute">·</span>
        <span className="normal-case tracking-normal text-dim">
          requested by <AgentName agent={agents.get(a.requested_by)} id={a.requested_by} className="text-[10px]" />
        </span>
      </div>
      <p className="scroll-thin mt-2 max-h-40 overflow-y-auto text-[11.5px] leading-relaxed whitespace-pre-line text-slate-300">{a.detail}</p>
      {a.proposed_action && (
        <div className="mt-2 rounded-md border border-edge bg-black/30 px-2.5 py-2">
          <p className="label !text-[8.5px]">Proposed action</p>
          <p className="mt-0.5 text-[11.5px] text-slate-200">{a.proposed_action}</p>
        </div>
      )}
      <label className="mt-3 block">
        <span className="font-mono text-[9.5px] font-semibold uppercase tracking-[0.16em] text-amber-200">Reply / note to the agent</span>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={question ? 3 : 2}
          autoFocus={question}
          placeholder={question ? "Answer the agent's question — it will continue with your reply…" : "Optional — conditions, corrections or context for the agent…"}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && busy === null) {
              e.preventDefault();
              void go("APPROVED");
            }
          }}
          aria-describedby={`${a.id}-hint`}
          className="mt-1 block w-full resize-y rounded-md border border-amber-500/30 bg-black/40 px-2.5 py-2 text-[12px] leading-relaxed text-ink placeholder:text-mute focus:border-amber-400/70 focus:ring-1 focus:ring-amber-400/30 focus:outline-none"
        />
        <span id={`${a.id}-hint`} className="mt-1 block text-right font-mono text-[8.5px] tracking-[0.08em] text-mute">
          Ctrl+Enter to {question ? "reply & continue" : "approve"}
        </span>
      </label>
      <div className="mt-1.5 grid grid-cols-2 gap-2">
        <button
          onClick={() => go("APPROVED")}
          disabled={busy !== null}
          className="h-8 rounded-md border border-emerald-400/60 bg-emerald-500/15 font-mono text-[10.5px] font-semibold uppercase tracking-[0.2em] text-emerald-300 transition hover:bg-emerald-500/25 disabled:opacity-50"
        >
          {busy === "APPROVED" ? "Sending…" : question ? (note.trim() ? "Reply & continue" : "Continue") : note.trim() ? "Approve + note" : "Approve"}
        </button>
        <button
          onClick={() => go("REJECTED")}
          disabled={busy !== null}
          className="h-8 rounded-md border border-red-400/50 bg-red-500/10 font-mono text-[10.5px] font-semibold uppercase tracking-[0.2em] text-red-300 transition hover:bg-red-500/20 disabled:opacity-50"
        >
          {busy === "REJECTED" ? "Rejecting…" : "Reject"}
        </button>
      </div>
      {err && <p className="mt-2 font-mono text-[10.5px] text-red-400">{err}</p>}
    </article>
  );
}

export function ApprovalQueue({
  approvals,
  agents,
  decide,
  className,
}: {
  approvals: ApprovalRequest[];
  agents: Map<string, AgentDefinition>;
  decide: (id: string, d: Decision, note?: string) => Promise<ApprovalRequest>;
  className?: string;
}) {
  const pending = approvals.filter((a) => a.state === "PENDING");
  const history = approvals.filter((a) => a.state !== "PENDING").sort((a, b) => (b.decided_at ?? "").localeCompare(a.decided_at ?? ""));
  const [showHistory, setShowHistory] = useState(false);

  return (
    <Panel
      code="05"
      title="Human intervention"
      className={cx(className, pending.length > 0 && "!border-amber-500/40")}
      meta={
        pending.length > 0 ? (
          <span className="font-semibold text-amber-300">{pending.length} pending</span>
        ) : (
          <span>{history.length} decided</span>
        )
      }
      bodyClassName="flex flex-col gap-2.5 p-3"
    >
      {pending.length === 0 && (
        <div className="flex items-center gap-3 rounded-lg border border-edge/70 bg-black/15 px-3 py-2.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-300">
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <div>
            <p className="text-[12px] text-slate-200">No intervention required</p>
            <p className="font-mono text-[9.5px] tracking-[0.08em] text-mute">Agents pause here before external, financial or irreversible actions.</p>
          </div>
        </div>
      )}
      {pending.map((a) => (
        <PendingCard key={a.id} a={a} agents={agents} decide={decide} />
      ))}
      {history.length > 0 && (
        <div>
          <button onClick={() => setShowHistory((v) => !v)} className="label flex w-full items-center justify-between px-1 py-1 !text-[9px] hover:!text-slate-300">
            <span>History · {history.length}</span>
            <span>{showHistory ? "−" : "+"}</span>
          </button>
          {(showHistory || pending.length === 0) && (
            <ul className="mt-1 grid gap-1">
              {history.map((a) => {
                const ok = a.state === "APPROVED";
                const color = ok ? "#22c55e" : a.state === "REJECTED" ? "#ef4444" : "#64748b";
                return (
                  <li key={a.id} className="flex items-start gap-2 rounded-md border border-edge/60 bg-black/15 px-2.5 py-1.5">
                    <span className="mt-[3px] font-mono text-[9px] tracking-[0.14em]" style={{ color }}>
                      {a.state}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[11.5px] text-slate-300">{a.title}</p>
                      {a.decision_note && <p className="truncate text-[10.5px] text-dim italic">“{a.decision_note}”</p>}
                    </div>
                    <span className="font-mono text-[9.5px] text-mute">{a.decided_at ? hms(a.decided_at) : ""}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}
