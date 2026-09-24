"use client";

import { useEffect, useState } from "react";
import { DRAFT_EML_URL, fileHref, type DraftDecisionBody } from "@/lib/api";
import type { EmailDraft, FollowUp } from "@/lib/contracts";
import { parseAddr } from "@/lib/followups";
import { cx } from "@/lib/ui";

const splitAddrs = (s: string) =>
  s
    .split(/[;,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

/**
 * Draft review (docs/INBOX.md §3): the human edits and approves (or discards) a follow-up email ALFRED drafted.
 * ATLAS never sends — approval saves it to Outlook Drafts or exports an unsent .eml.
 */
export function DraftDrawer({
  draft,
  followup,
  onClose,
  decide,
}: {
  draft: EmailDraft | null;
  followup: FollowUp | null;
  onClose: () => void;
  decide: (id: string, body: DraftDecisionBody) => Promise<EmailDraft>;
}) {
  const open = !!draft;
  const [to, setTo] = useState("");
  const [cc, setCc] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState<"APPROVED" | "DISCARDED" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Reset the form when a different draft opens (not on every upsert of the same one).
  const id = draft?.id ?? null;
  useEffect(() => {
    if (!draft) return;
    setTo(draft.to.join(", "));
    setCc(draft.cc.join(", "));
    setSubject(draft.subject);
    setBody(draft.body);
    setBusy(null);
    setErr(null);
  }, [id]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const editable = draft?.status === "PROPOSED";
  const approved = draft?.status === "APPROVED" || draft?.status === "EXPORTED";
  const discarded = draft?.status === "DISCARDED";
  const toList = splitAddrs(to);

  async function go(decision: "APPROVED" | "DISCARDED") {
    if (!draft) return;
    setBusy(decision);
    setErr(null);
    try {
      const b: DraftDecisionBody =
        decision === "APPROVED" ? { decision, to: toList, cc: splitAddrs(cc), subject: subject.trim(), body } : { decision };
      await decide(draft.id, b);
      setBusy(null);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Decision failed");
      setBusy(null);
    }
  }

  const outlook = draft?.export === "outlook_drafts" && draft.download_url ? draft.download_url : null;
  const emlHref = draft ? fileHref(draft.download_url) ?? DRAFT_EML_URL(draft.id) : undefined;

  const field = "block w-full rounded-md border border-edge bg-black/35 px-3 text-[13px] text-ink placeholder:text-mute focus:border-signal/60 focus:ring-1 focus:ring-signal/25 focus:outline-none disabled:opacity-70";

  return (
    <div className={cx("fixed inset-0 z-40", open ? "pointer-events-auto" : "pointer-events-none")} aria-hidden={!open}>
      <div className={cx("absolute inset-0 bg-black/55 backdrop-blur-[2px] transition-opacity", open ? "opacity-100" : "opacity-0")} onClick={onClose} />
      <aside
        role="dialog"
        aria-label="Draft review"
        className={cx(
          "absolute top-0 right-0 flex h-full w-full max-w-[620px] flex-col border-l border-edge bg-panel shadow-[-20px_0_60px_-20px_rgba(0,0,0,0.9)] transition-transform duration-200",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-edge/80 px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="label flex items-center gap-2 !text-slate-300">
              <span className="text-signal/60">11</span>
              <span className="text-signal/30">//</span>
              Draft review
              {draft && (
                <span
                  className={cx(
                    "rounded border px-1.5 py-[1px] text-[8.5px] tracking-[0.16em]",
                    editable ? "border-emerald-400/40 text-emerald-300" : approved ? "border-signal/40 text-signal" : "border-edge-2 text-dim",
                  )}
                >
                  {editable ? "Proposed" : approved ? "Approved" : "Discarded"}
                </span>
              )}
            </h2>
            {followup && (
              <p className="mt-1.5 truncate text-[12px] text-slate-400">
                Follow-up: <span className="text-slate-200">{followup.title}</span>
                {followup.counterpart && <span className="text-mute"> · {parseAddr(followup.counterpart).name}</span>}
              </p>
            )}
          </div>
          <button onClick={onClose} className="rounded p-1.5 text-dim hover:bg-white/[0.05] hover:text-slate-200" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
              <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />
            </svg>
          </button>
        </header>

        {draft && (
          <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">
            <div className="grid grid-cols-[64px_minmax(0,1fr)] items-center gap-x-3 gap-y-2">
              <label htmlFor="drf-to" className="label !text-[9.5px]">
                To
              </label>
              <input id="drf-to" value={to} onChange={(e) => setTo(e.target.value)} disabled={!editable} className={cx(field, "h-9")} placeholder="name@company.com" />
              <label htmlFor="drf-cc" className="label !text-[9.5px]">
                Cc
              </label>
              <input id="drf-cc" value={cc} onChange={(e) => setCc(e.target.value)} disabled={!editable} className={cx(field, "h-9")} placeholder="Optional" />
              <label htmlFor="drf-subject" className="label !text-[9.5px]">
                Subject
              </label>
              <input id="drf-subject" value={subject} onChange={(e) => setSubject(e.target.value)} disabled={!editable} className={cx(field, "h-9 font-medium")} />
            </div>
            <textarea
              aria-label="Body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              disabled={!editable}
              rows={14}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && editable && busy === null && toList.length) {
                  e.preventDefault();
                  void go("APPROVED");
                }
              }}
              className={cx(field, "min-h-[280px] flex-1 resize-y py-3 font-sans text-[14px] leading-[1.65]")}
            />
            {editable && <p className="font-mono text-[10px] tracking-[0.06em] text-mute">
              ATLAS never sends email. Approving saves this as an <span className="text-slate-400">unsent draft</span> for you to send from Outlook.
            </p>}
          </div>
        )}

        {draft && (
          <footer className="border-t border-edge/80 px-5 py-3.5">
            {editable && (
              <>
                <div className="grid grid-cols-[1fr_auto] gap-2">
                  <button
                    onClick={() => go("APPROVED")}
                    disabled={busy !== null || !toList.length || !subject.trim()}
                    className="h-9 rounded-md border border-emerald-400/60 bg-emerald-500/15 font-mono text-[11px] font-semibold uppercase tracking-[0.2em] text-emerald-300 transition hover:bg-emerald-500/25 disabled:opacity-50"
                  >
                    {busy === "APPROVED" ? "Saving draft…" : "Approve draft"}
                  </button>
                  <button
                    onClick={() => go("DISCARDED")}
                    disabled={busy !== null}
                    className="h-9 rounded-md border border-red-400/40 bg-red-500/[0.08] px-5 font-mono text-[11px] font-semibold uppercase tracking-[0.2em] text-red-300 transition hover:bg-red-500/15 disabled:opacity-50"
                  >
                    {busy === "DISCARDED" ? "Discarding…" : "Discard"}
                  </button>
                </div>
                <p className="mt-1.5 text-right font-mono text-[9px] tracking-[0.08em] text-mute">Ctrl+Enter to approve · Esc to close</p>
              </>
            )}
            {approved && (
              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-signal/30 bg-signal/[0.06] px-3.5 py-3">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-300">
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
                    <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12.5px] text-slate-200">{outlook ? "Saved to your Outlook Drafts" : "Approved — ready as an .eml file"}</p>
                  <p className="font-mono text-[10px] tracking-[0.04em] text-dim">
                    {outlook ? "Review and send it from Outlook." : "Opens in Outlook as an unsent draft."}
                  </p>
                </div>
                {outlook ? (
                  <a
                    href={outlook}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex h-8 items-center rounded-md border border-signal/50 bg-signal/10 px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] text-signal hover:bg-signal/20"
                  >
                    Open in Outlook
                  </a>
                ) : (
                  <a
                    href={emlHref}
                    download={`${(draft.subject || "draft").replace(/[\\/:*?"<>|]+/g, " ").slice(0, 80)}.eml`}
                    className="inline-flex h-8 items-center rounded-md border border-signal/50 bg-signal/10 px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] text-signal hover:bg-signal/20"
                  >
                    Download .eml
                  </a>
                )}
              </div>
            )}
            {discarded && <p className="font-mono text-[11px] text-dim">This draft was discarded. Use “Draft” on the follow-up card to ask ALFRED for a new one.</p>}
            {err && <p className="mt-2 font-mono text-[10.5px] text-red-400">{err}</p>}
          </footer>
        )}
      </aside>
    </div>
  );
}
