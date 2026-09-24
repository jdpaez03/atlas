"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { INBOX_SETUP_DOC, type InboxApi, type InboxConnectStart, type InboxStatus } from "@/lib/api";
import { relTime } from "@/lib/followups";
import { createPortal } from "react-dom";
import { useNow } from "@/lib/ui";

const scheduleText = (s: InboxStatus["schedule"]) => (Array.isArray(s) ? s.join(", ") : s ? s.split(",").map((x) => x.trim()).join(", ") : "—");

/* ------------------------------------------------------------------------------------------------ connect modal */

function ConnectModal({ inbox, onClose, onConnected }: { inbox: InboxApi; onClose: () => void; onConnected: () => void }) {
  const [start, setStart] = useState<InboxConnectStart | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [state, setState] = useState<"starting" | "pending" | "connected" | "error">("starting");
  const [account, setAccount] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const now = useNow(1000);

  const begin = useCallback(() => {
    setState("starting");
    setErr(null);
    inbox
      .connect()
      .then((s) => {
        setStart(s);
        setStartedAt(Date.now());
        setState("pending");
      })
      .catch((x) => {
        setErr(x instanceof Error ? x.message : "Could not start sign-in");
        setState("error");
      });
  }, [inbox]);

  useEffect(() => begin(), [begin]);

  // Poll until the user finishes signing in.
  useEffect(() => {
    if (state !== "pending") return;
    let alive = true;
    const t = setInterval(() => {
      inbox
        .connectStatus()
        .then((s) => {
          if (!alive) return;
          if (s.state === "connected") {
            setAccount(s.account);
            setState("connected");
            onConnected();
          } else if (s.state === "error") {
            setErr(s.error);
            setState("error");
          }
        })
        .catch(() => undefined);
    }, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [state, inbox, onConnected]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const left = start && startedAt && now ? Math.max(0, Math.round(start.expires_in - (now - startedAt) / 1000)) : null;
  const expired = state === "pending" && left === 0;

  async function copy() {
    if (!start) return;
    try {
      await navigator.clipboard.writeText(start.user_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked — the code is selectable */
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="Connect Microsoft 365">
      <div className="absolute inset-0 bg-black/65 backdrop-blur-[3px]" onClick={onClose} />
      <div className="panel relative w-full max-w-[480px] !bg-panel p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="label !text-signal/80">Connect inbox</p>
            <h2 className="mt-1 text-[17px] font-medium text-ink">Sign in to Microsoft 365</h2>
          </div>
          <button onClick={onClose} className="rounded p-1.5 text-dim hover:bg-white/[0.05] hover:text-slate-200" aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
              <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />
            </svg>
          </button>
        </div>

        {state === "connected" ? (
          <div className="mt-6 flex flex-col items-center gap-3 py-4 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full border border-emerald-500/50 bg-emerald-500/10 text-emerald-300">
              <svg width="20" height="20" viewBox="0 0 16 16" fill="none" aria-hidden>
                <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <p className="text-[14px] text-slate-200">Connected{account ? ` as ${account}` : ""}</p>
            <p className="text-[12px] text-dim">HERMES will read new mail on the next scan. ATLAS never sends email.</p>
            <button onClick={onClose} className="mt-2 h-8 rounded-md border border-edge-2 px-4 font-mono text-[10.5px] uppercase tracking-[0.16em] text-slate-200 hover:bg-white/[0.05]">
              Done
            </button>
          </div>
        ) : state === "error" || expired ? (
          <div className="mt-5">
            <p className="rounded-md border border-red-500/40 bg-red-500/[0.08] px-3 py-2.5 text-[12.5px] leading-relaxed text-red-200">
              {expired ? "The code expired before sign-in finished." : err ?? "Sign-in failed."}
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <a href={INBOX_SETUP_DOC} target="_blank" rel="noreferrer" className="h-8 rounded-md px-3 font-mono text-[10.5px] uppercase leading-8 tracking-[0.14em] text-dim hover:text-slate-200">
                Setup guide
              </a>
              <button onClick={begin} className="h-8 rounded-md border border-signal/50 bg-signal/10 px-4 font-mono text-[10.5px] uppercase tracking-[0.16em] text-signal hover:bg-signal/20">
                Try again
              </button>
            </div>
          </div>
        ) : (
          <>
            <ol className="mt-4 grid gap-1 text-[12.5px] leading-relaxed text-slate-400">
              <li>
                <span className="font-mono text-signal/70">1.</span> Open{" "}
                {start ? (
                  <a href={start.verification_uri} target="_blank" rel="noreferrer" className="text-signal underline decoration-signal/40 underline-offset-2 hover:decoration-signal">
                    {start.verification_uri.replace(/^https?:\/\//, "")}
                  </a>
                ) : (
                  "the Microsoft sign-in page"
                )}
              </li>
              <li>
                <span className="font-mono text-signal/70">2.</span> Enter this code and sign in with your work account
              </li>
            </ol>
            <div className="mt-4 flex items-center gap-3 rounded-lg border border-signal/30 bg-black/40 px-4 py-4">
              <code className="min-w-0 flex-1 text-center font-mono text-[40px] leading-none font-semibold tracking-[0.18em] text-ink select-all" aria-live="polite">
                {start?.user_code ?? "········"}
              </code>
              <button
                onClick={copy}
                disabled={!start}
                className="h-9 shrink-0 rounded-md border border-edge-2 px-3 font-mono text-[10px] uppercase tracking-[0.16em] text-slate-200 hover:bg-white/[0.05] disabled:opacity-50"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            {start && (
              <a
                href={start.verification_uri}
                target="_blank"
                rel="noreferrer"
                className="mt-3 flex h-10 items-center justify-center gap-2 rounded-md border border-signal/50 bg-signal/10 font-mono text-[11px] font-semibold uppercase tracking-[0.18em] text-signal hover:bg-signal/20"
              >
                Open sign-in page
                <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M12 9.5v3.75H2.75V4H6.5" />
                </svg>
              </a>
            )}
            <div className="mt-4 flex items-center justify-between font-mono text-[10px] tracking-[0.06em] text-mute">
              <span className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400 dot-live" style={{ ["--c" as string]: "#f59e0baa" }} />
                {state === "starting" ? "Requesting a code…" : "Waiting for you to sign in…"}
              </span>
              {left != null && (
                <span className="tabular-nums">
                  expires in {Math.floor(left / 60)}:{(left % 60).toString().padStart(2, "0")}
                </span>
              )}
            </div>
            <p className="mt-3 border-t border-edge/70 pt-3 text-[11px] leading-relaxed text-dim">
              ATLAS asks for read access to mail and, optionally, permission to save drafts. It never sends email.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ chip */

/**
 * Inbox status chip (docs/INBOX.md §3). Hidden when the backend has no /inbox/* (404).
 */
export function InboxChip({ inbox, ready, conn }: { inbox: InboxApi; ready: boolean; conn: string }) {
  const [status, setStatus] = useState<InboxStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanErr, setScanErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const now = useNow(30_000);

  const refresh = useCallback(() => {
    if (!ready) return;
    inbox.status().then(setStatus);
  }, [inbox, ready]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => clearInterval(t);
  }, [refresh, conn]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!status) return null;

  const configured = !!status.source;
  const connected = configured && status.connected;
  const canDeviceLogin = status.source === "graph";
  const tone = connected ? "#22c55e" : configured ? "#f59e0b" : "#64748b";
  const label = connected ? (status.account?.split("@")[0] ?? "Connected") : configured ? (canDeviceLogin ? "Connect" : "Not connected") : "Not set up";

  async function scan() {
    setScanning(true);
    setScanErr(null);
    try {
      await inbox.scan();
      setTimeout(refresh, 2500);
      setTimeout(refresh, 6000);
    } catch (x) {
      setScanErr(x instanceof Error ? x.message : "Scan failed");
    } finally {
      setTimeout(() => setScanning(false), 1500);
    }
  }

  const onChip = () => {
    if (configured && !connected && canDeviceLogin) setConnecting(true);
    else setOpen((v) => !v);
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={onChip}
        className="flex h-[26px] items-center gap-2 rounded-full border px-2.5 font-mono text-[10px] uppercase tracking-[0.16em] transition hover:brightness-125"
        style={{ color: tone, borderColor: `${tone}44`, background: `${tone}10` }}
        title={connected ? `Inbox connected as ${status.account ?? "—"}` : status.hint ?? (configured ? "Inbox not connected" : "Inbox not configured")}
        aria-haspopup="dialog"
        aria-expanded={open || connecting}
      >
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.25" />
          <path d="M2.25 4l5.75 4.5L13.75 4" />
        </svg>
        <span className="hidden max-w-[120px] truncate normal-case tracking-[0.06em] sm:inline">
          {connected ? label : <span className="uppercase tracking-[0.16em]">{label}</span>}
        </span>
        {scanning && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 dot-live" style={{ ["--c" as string]: "#22c55eaa" }} />}
      </button>

      {open && (
        <div role="dialog" aria-label="Inbox status" className="absolute top-full right-0 z-40 mt-2 w-[300px] rounded-lg border border-edge-2 bg-panel-2 p-3.5 shadow-[0_24px_50px_-16px_rgba(0,0,0,0.95)]">
          {connected ? (
            <>
              <p className="label !text-[9px]">Inbox · {status.source === "graph" ? "Microsoft 365" : "Mail folder"}</p>
              <p className="mt-1 truncate text-[13px] text-ink" title={status.account ?? undefined}>
                {status.account ?? "Connected"}
              </p>
              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 font-mono text-[10.5px]">
                <dt className="text-mute">Last scan</dt>
                <dd className="text-right text-slate-300" title={status.last_scan ?? undefined}>
                  {now ? relTime(status.last_scan, now) : "—"}
                </dd>
                <dt className="text-mute">Next scan</dt>
                <dd className="text-right text-slate-300" title={status.next_scan ?? undefined}>
                  {now ? relTime(status.next_scan, now) : "—"}
                </dd>
                <dt className="text-mute">Schedule</dt>
                <dd className="text-right text-slate-300">{scheduleText(status.schedule)}</dd>
                <dt className="text-mute">Emails read</dt>
                <dd className="text-right text-slate-300 tabular-nums">{status.processed_count}</dd>
              </dl>
              <button
                onClick={scan}
                disabled={scanning}
                className="mt-3 h-8 w-full rounded-md border border-signal/50 bg-signal/10 font-mono text-[10.5px] font-semibold uppercase tracking-[0.18em] text-signal hover:bg-signal/20 disabled:opacity-60"
              >
                {scanning ? "Scanning…" : "Scan now"}
              </button>
              {scanErr && <p className="mt-1.5 font-mono text-[10px] text-red-400">{scanErr}</p>}
              {status.hint && <p className="mt-2 text-[11px] leading-relaxed text-amber-200/80">{status.hint}</p>}
            </>
          ) : (
            <>
              <p className="label !text-[9px]">Inbox · {configured ? "not connected" : "not configured"}</p>
              <p className="mt-1.5 text-[12px] leading-relaxed text-slate-300">
                {status.hint ??
                  (configured
                    ? "The mail source is configured but not reachable."
                    : "Connect Microsoft 365 or a Power Automate mail folder so HERMES can track follow-ups from your work email.")}
              </p>
              <a
                href={INBOX_SETUP_DOC}
                target="_blank"
                rel="noreferrer"
                className="mt-3 inline-flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-signal hover:underline"
              >
                Inbox setup guide
                <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                  <path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M12 9.5v3.75H2.75V4H6.5" />
                </svg>
              </a>
            </>
          )}
        </div>
      )}

      {connecting &&
        createPortal(
        <ConnectModal
          inbox={inbox}
          onClose={() => {
            setConnecting(false);
            refresh();
          }}
          onConnected={refresh}
        />,
          document.body,
        )}
    </div>
  );
}

