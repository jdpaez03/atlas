"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { INBOX_SETUP_DOC, type InboxApi, type InboxStatus } from "@/lib/api";
import { relTime } from "@/lib/followups";
import { createPortal } from "react-dom";
import { useNow } from "@/lib/ui";
import { DeviceCodeModal, type DeviceCodeCopy } from "./DeviceCodeModal";

const INBOX_CONNECT_COPY: DeviceCodeCopy = {
  eyebrow: "Connect inbox",
  title: "Sign in to Microsoft 365",
  account: "your work account",
  connected: "HERMES will read new mail on the next scan. ATLAS never sends email.",
  footer: "ATLAS asks for read access to mail and, optionally, permission to save drafts. It never sends email.",
  setupDoc: INBOX_SETUP_DOC,
};

const scheduleText = (s: InboxStatus["schedule"]) => (Array.isArray(s) ? s.join(", ") : s ? s.split(",").map((x) => x.trim()).join(", ") : "—");


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
        <DeviceCodeModal
          start={inbox.connect}
          poll={inbox.connectStatus}
          copy={INBOX_CONNECT_COPY}
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

