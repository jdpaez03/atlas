"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { FollowUpPatch } from "@/lib/api";
import type { EmailDraft, FollowUp } from "@/lib/contracts";
import {
  parseDue,
  COLUMNS,
  KIND_LABEL,
  dateInputToIso,
  dueLabel,
  followupCounts,
  isOpen,
  isOverdue,
  parseAddr,
  proposedDrafts,
  shortDate,
  sortFollowups,
  toDateInput,
} from "@/lib/followups";
import { PRIORITY, cx, useNow } from "@/lib/ui";
import { Tag } from "./primitives";

type Busy = "done" | "dismiss" | "snooze" | "draft" | "reopen" | null;

export interface FollowUpActions {
  patch: (id: string, patch: FollowUpPatch) => Promise<unknown>;
  draft: (id: string) => Promise<unknown>;
  openDraft: (id: string) => void;
}

/* ------------------------------------------------------------------------------------------------ icons */

const Mail = ({ size = 12, className }: { size?: number; className?: string }) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
    <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.25" />
    <path d="M2.25 4l5.75 4.5L13.75 4" />
  </svg>
);
const Ext = () => (
  <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M12 9.5v3.75H2.75V4H6.5" />
  </svg>
);

/* ------------------------------------------------------------------------------------------------ snooze */

function SnoozeMenu({ current, onPick, onClose }: { current: string | null; onPick: (iso: string) => void; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const today = new Date();
  const plus = (n: number) => {
    const d = new Date(today);
    d.setDate(d.getDate() + n);
    return d;
  };
  const nextMonday = (() => {
    const d = new Date(today);
    d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
    return d;
  })();
  const [val, setVal] = useState(toDateInput(current && new Date(current) > today ? new Date(current) : plus(1)));
  useEffect(() => {
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && onClose();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  const quick: [string, Date][] = [
    ["Tomorrow", plus(1)],
    ["In 3 days", plus(3)],
    ["Next Monday", nextMonday],
  ];
  return (
    <div ref={ref} role="dialog" aria-label="Snooze until" className="absolute top-full left-0 z-30 mt-1.5 w-[220px] rounded-lg border border-edge-2 bg-panel-2 p-2.5 shadow-[0_18px_40px_-12px_rgba(0,0,0,0.9)]">
      <p className="label !text-[9px]">Snooze until</p>
      <div className="mt-1.5 grid gap-1">
        {quick.map(([label, d]) => (
          <button
            key={label}
            onClick={() => onPick(dateInputToIso(toDateInput(d)))}
            className="flex items-center justify-between rounded-md px-2 py-1 text-left text-[12px] text-slate-200 hover:bg-white/[0.05]"
          >
            {label}
            <span className="font-mono text-[10px] text-mute">{d.toLocaleDateString("es-MX", { weekday: "short", day: "numeric", month: "short" })}</span>
          </button>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-1.5 border-t border-edge/70 pt-2">
        <input
          type="date"
          value={val}
          min={toDateInput(today)}
          onChange={(e) => setVal(e.target.value)}
          className="h-7 min-w-0 flex-1 rounded-md border border-edge bg-black/40 px-2 font-mono text-[11px] text-ink [color-scheme:dark] focus:border-signal/60 focus:outline-none"
          aria-label="Pick a date"
        />
        <button
          onClick={() => val && onPick(dateInputToIso(val))}
          className="h-7 rounded-md border border-signal/50 bg-signal/10 px-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-signal hover:bg-signal/20"
        >
          Set
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ card */

function FollowUpCard({ f, draft, now, actions }: { f: FollowUp; draft: EmailDraft | undefined; now: number; actions: FollowUpActions }) {
  const [busy, setBusy] = useState<Busy>(null);
  const [err, setErr] = useState<string | null>(null);
  const [snooze, setSnooze] = useState(false);
  const open = isOpen(f);
  const overdue = now ? isOverdue(f, now) : false;
  const due = f.due && now ? dueLabel(f.due, now) : null;
  const who = parseAddr(f.counterpart);
  const src = f.source;
  const pr = PRIORITY[f.priority] ?? PRIORITY.MEDIUM;
  const draftReady = draft?.status === "PROPOSED";
  const drafting = busy === "draft" && !draftReady;

  useEffect(() => {
    if (draftReady && busy === "draft") setBusy(null);
  }, [draftReady, busy]);

  async function run(kind: Exclude<Busy, null>, fn: () => Promise<unknown>, keepBusy = false) {
    setBusy(kind);
    setErr(null);
    try {
      await fn();
      if (!keepBusy) setBusy(null);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Action failed");
      setBusy(null);
    }
  }

  const dueColor = !due ? "#4a5670" : due.tone === "late" ? "#f87171" : due.tone === "today" ? "#fbbf24" : due.tone === "soon" ? "#e2e8f0" : "#94a3b8";

  return (
    <article
      className={cx(
        "group/card @container relative rounded-lg border bg-black/20 p-3 transition",
        !open ? "border-edge/50 opacity-55" : overdue ? "border-red-500/40 bg-red-500/[0.04]" : "border-edge/80 hover:border-edge-2",
      )}
    >
      {overdue && <span className="absolute top-2 bottom-2 left-0 w-[2px] rounded-full bg-red-400/80" aria-hidden />}
      <div className="flex items-center gap-1.5 whitespace-nowrap">
        <Tag color={pr.color} className="!text-[8.5px] shrink-0">
          {pr.label}
        </Tag>
        <span className="min-w-0 truncate font-mono text-[9px] uppercase tracking-[0.12em] text-mute">
          {KIND_LABEL[f.kind]}
          {f.status === "WAITING" && open && <span className="text-amber-300/80"> · chasing</span>}
          {!open && <span className="text-dim"> · {f.status.toLowerCase()}</span>}
        </span>
        <span
          className="ml-auto inline-flex shrink-0 items-center gap-1 font-mono text-[10px] tabular-nums"
          style={{ color: open ? dueColor : "#4a5670" }}
          title={f.due ? parseDue(f.due).toLocaleDateString("es-MX", { dateStyle: "full" }) : "No due date"}
        >
          {overdue && <span className="h-1.5 w-1.5 rounded-full bg-red-400" />}
          {due ? due.text : "no date"}
        </span>
      </div>

      <h3 className={cx("mt-1.5 text-[13.5px] leading-snug font-medium", open ? "text-ink" : "text-slate-400 line-through decoration-slate-600")}>{f.title}</h3>
      <p className="mt-0.5 truncate text-[11.5px] text-slate-400" title={f.counterpart ?? undefined}>
        <span className="text-slate-300">{who.name}</span>
        {who.email && who.email !== who.name && <span className="text-mute"> · {who.email}</span>}
      </p>

      {src && (
        <div className="group/src relative mt-2 flex items-center gap-1.5 rounded-md border border-edge/60 bg-black/25 px-2 py-1.5">
          <Mail size={11} className="shrink-0 text-dim" />
          <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400" tabIndex={0} aria-describedby={`${f.id}-ex`}>
            <span className="text-slate-300">{src.subject}</span>
            <span className="text-mute">
              {" "}
              · {parseAddr(src.sender).name} · {shortDate(src.received_at)}
            </span>
          </span>
          {src.web_link && (
            <a
              href={src.web_link}
              target="_blank"
              rel="noreferrer"
              className="inline-flex shrink-0 items-center gap-1 rounded px-1 font-mono text-[9px] uppercase tracking-[0.12em] text-signal/80 hover:text-signal"
              title="Open the source email in Outlook"
            >
              Outlook <Ext />
            </a>
          )}
          {src.excerpt && (
            <div
              id={`${f.id}-ex`}
              role="tooltip"
              className="pointer-events-none invisible absolute top-full right-0 left-0 z-20 mt-1 rounded-md border border-edge-2 bg-panel-2 px-3 py-2 text-[11.5px] leading-relaxed text-slate-300 opacity-0 shadow-[0_18px_40px_-12px_rgba(0,0,0,0.9)] transition-opacity group-focus-within/src:visible group-focus-within/src:opacity-100 group-hover/src:visible group-hover/src:opacity-100"
            >
              <span className="label mb-1 block !text-[8.5px]">Excerpt</span>“{src.excerpt}”
            </div>
          )}
        </div>
      )}

      {open && draftReady && (
        <button
          onClick={() => actions.openDraft(draft!.id)}
          className="mt-2 flex w-full items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/[0.08] px-2 py-1.5 text-left transition hover:bg-emerald-500/15"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 dot-live" style={{ ["--c" as string]: "#34d399aa" }} />
          <span className="min-w-0 flex-1 truncate text-[11.5px] text-emerald-200">Draft ready — {draft!.subject}</span>
          <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-emerald-300">Review →</span>
        </button>
      )}

      <div className="mt-2.5 flex flex-wrap items-center gap-1">
        {open ? (
          <>
            <ActionBtn onClick={() => run("done", () => actions.patch(f.id, { status: "DONE" }))} busy={busy === "done"} tone="ok">
              Done
            </ActionBtn>
            <ActionBtn onClick={() => run("dismiss", () => actions.patch(f.id, { status: "DISMISSED" }))} busy={busy === "dismiss"}>
              Dismiss
            </ActionBtn>
            <div className="relative">
              <ActionBtn onClick={() => setSnooze((v) => !v)} busy={busy === "snooze"} active={snooze}>
                Snooze
              </ActionBtn>
              {snooze && (
                <SnoozeMenu
                  current={f.due}
                  onClose={() => setSnooze(false)}
                  onPick={(iso) => {
                    setSnooze(false);
                    void run("snooze", () => actions.patch(f.id, { due: iso }));
                  }}
                />
              )}
            </div>
            <span className="flex-1" />
            {!draftReady && (
              <ActionBtn onClick={() => run("draft", () => actions.draft(f.id), true)} busy={drafting} tone="signal" busyLabel="Drafting…" title="Ask ALFRED to draft a follow-up email">
                <>
                  Draft<span className="hidden @[310px]:inline"> follow-up</span>
                </>
              </ActionBtn>
            )}
          </>
        ) : (
          <ActionBtn onClick={() => run("reopen", () => actions.patch(f.id, { status: "OPEN" }))} busy={busy === "reopen"}>
            Reopen
          </ActionBtn>
        )}
      </div>
      {err && <p className="mt-1.5 font-mono text-[10px] text-red-400">{err}</p>}
    </article>
  );
}

function ActionBtn({
  children,
  onClick,
  busy,
  busyLabel,
  tone,
  active,
  title,
}: {
  children: ReactNode;
  title?: string;
  onClick: () => void;
  busy?: boolean;
  busyLabel?: string;
  tone?: "ok" | "signal";
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      title={title}
      className={cx(
        "h-6 whitespace-nowrap rounded-md border px-[7px] font-mono text-[9.5px] uppercase tracking-[0.08em] transition disabled:opacity-60",
        tone === "ok"
          ? "border-emerald-500/35 text-emerald-300 hover:bg-emerald-500/10"
          : tone === "signal"
            ? "border-signal/40 text-signal hover:bg-signal/10"
            : active
              ? "border-edge-2 bg-white/[0.06] text-slate-200"
              : "border-edge text-dim hover:border-edge-2 hover:text-slate-200",
      )}
    >
      {busy ? busyLabel ?? "…" : children}
    </button>
  );
}

/* ------------------------------------------------------------------------------------------------ view */

export function FollowUpsBoard({
  followups,
  drafts,
  actions,
  inboxAvailable,
  className,
}: {
  followups: FollowUp[];
  drafts: EmailDraft[];
  actions: FollowUpActions;
  inboxAvailable: boolean;
  className?: string;
}) {
  const now = useNow(60_000);
  const [showClosed, setShowClosed] = useState(false);
  const draftsById = new Map(drafts.map((d) => [d.id, d]));
  const counts = followupCounts(followups, now);
  const closed = followups.filter((f) => !isOpen(f)).length;
  const pending = proposedDrafts(drafts).length;

  return (
    <section className={cx("panel flex min-w-0 flex-col", className)}>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge/80 px-4 py-2.5">
        <h2 className="label flex items-center gap-2 !text-slate-300">
          <span className="text-signal/60">10</span>
          <span className="text-signal/30">//</span>
          Follow-ups
          <span className="normal-case tracking-normal text-mute">· from your work email</span>
        </h2>
        <div className="flex items-center gap-3 font-mono text-[10.5px] text-dim">
          <span>
            <span className="text-slate-200">{counts.open}</span> open
          </span>
          <span className={counts.overdue ? "text-red-400" : undefined}>
            <span className={counts.overdue ? "font-semibold" : "text-slate-200"}>{counts.overdue}</span> overdue
          </span>
          {pending > 0 && (
            <span className="text-emerald-300">
              <span className="font-semibold">{pending}</span> draft{pending === 1 ? "" : "s"} to review
            </span>
          )}
          {closed > 0 && (
            <button
              onClick={() => setShowClosed((v) => !v)}
              className="rounded border border-edge px-1.5 py-[1px] text-[9.5px] uppercase tracking-[0.14em] text-dim hover:border-edge-2 hover:text-slate-200"
              aria-pressed={showClosed}
            >
              {showClosed ? "Hide" : "Show"} closed · {closed}
            </button>
          )}
        </div>
      </header>

      {followups.length === 0 ? (
        <div className="flex min-h-[320px] flex-col items-center justify-center gap-2 px-6 py-10 text-center">
          <Mail size={22} className="text-mute" />
          <p className="text-[13px] text-slate-300">No follow-ups yet</p>
          <p className="max-w-sm font-mono text-[10.5px] leading-relaxed tracking-wide text-mute">
            {inboxAvailable
              ? "HERMES turns commitments and unanswered emails into follow-ups on each inbox scan. Use “Scan now” in the Inbox chip above."
              : "Connect a mailbox to let HERMES track commitments and pending replies from your work email."}
          </p>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 md:grid-cols-3">
          {COLUMNS.map((c) => {
            const items = sortFollowups(followups.filter((f) => c.kinds.includes(f.kind) && (showClosed || isOpen(f))));
            const colOpen = items.filter(isOpen).length;
            const colLate = now ? items.filter((f) => isOverdue(f, now)).length : 0;
            return (
              <div key={c.id} className="flex min-w-0 flex-col rounded-lg border border-edge/60 bg-black/10">
                <div className="border-b border-edge/60 px-3 py-2">
                  <div className="flex items-center gap-2 whitespace-nowrap">
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: c.color }} />
                    <h3 className="truncate font-mono text-[10.5px] font-semibold uppercase tracking-[0.16em] text-slate-200">{c.title}</h3>
                    <span className="rounded-full bg-white/[0.06] px-1.5 font-mono text-[9.5px] leading-[16px] text-slate-300 tabular-nums">{colOpen}</span>
                    {colLate > 0 && <span className="rounded-full bg-red-500/15 px-1.5 font-mono text-[9.5px] leading-[16px] text-red-300 tabular-nums">{colLate} late</span>}
                  </div>
                  <p className="mt-0.5 truncate pl-3.5 text-[10.5px] text-mute">{c.hint}</p>
                </div>
                <div className="flex flex-col gap-2 p-2">
                  {items.length === 0 ? (
                    <p className="px-2 py-6 text-center font-mono text-[10.5px] tracking-wide text-mute">Nothing here.</p>
                  ) : (
                    items.map((f) => <FollowUpCard key={f.id} f={f} draft={f.draft_id ? draftsById.get(f.draft_id) : undefined} now={now} actions={actions} />)
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
