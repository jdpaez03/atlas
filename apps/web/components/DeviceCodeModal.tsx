"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiErrorText, type InboxConnectStart, type InboxConnectState } from "@/lib/api";
import { useNow } from "@/lib/ui";

export interface DeviceCodeCopy {
  /** small label above the title, e.g. "Connect inbox" */
  eyebrow: string;
  title: string;
  /** what to sign in with, step 2 */
  account: string;
  /** shown under the check mark once connected */
  connected: string;
  /** the privacy line under the code */
  footer: string;
  /** "Setup guide" link on errors */
  setupDoc?: string;
}

/**
 * Microsoft device-code sign-in (the inbox and the PAGA Suite): a large code with Copy, the link and a
 * countdown; polls every 3 s. `closeOnConnected` skips the success screen.
 */
export function DeviceCodeModal({
  start: startFlow,
  poll,
  copy: text,
  onClose,
  onConnected,
  closeOnConnected,
}: {
  start: () => Promise<InboxConnectStart>;
  poll: () => Promise<InboxConnectState>;
  copy: DeviceCodeCopy;
  onClose: () => void;
  onConnected: (account: string | null) => void;
  closeOnConnected?: boolean;
}) {
  const [start, setStart] = useState<InboxConnectStart | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [state, setState] = useState<"starting" | "pending" | "connected" | "error">("starting");
  const [account, setAccount] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const now = useNow(1000);
  // Callbacks in a ref, so an inline onClose doesn't restart the 3 s poll on every render.
  const cb = useRef({ onConnected, onClose, closeOnConnected });
  cb.current = { onConnected, onClose, closeOnConnected };

  const begin = useCallback(() => {
    setState("starting");
    setErr(null);
    startFlow()
      .then((s) => {
        setStart(s);
        setStartedAt(Date.now());
        setState("pending");
      })
      .catch((x) => {
        setErr(apiErrorText(x, "Could not start sign-in"));
        setState("error");
      });
  }, [startFlow]);

  useEffect(() => begin(), [begin]);

  // Poll until the user finishes signing in.
  useEffect(() => {
    if (state !== "pending") return;
    let alive = true;
    const t = setInterval(() => {
      poll()
        .then((s) => {
          if (!alive) return;
          if (s.state === "connected") {
            setAccount(s.account);
            setState("connected");
            cb.current.onConnected(s.account);
            if (cb.current.closeOnConnected) cb.current.onClose();
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
  }, [state, poll]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const left = start && startedAt && now ? Math.max(0, Math.round(start.expires_in - (now - startedAt) / 1000)) : null;
  const expired = state === "pending" && left === 0;

  async function copyCode() {
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
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={text.title}>
      <div className="absolute inset-0 bg-black/65 backdrop-blur-[3px]" onClick={onClose} />
      <div className="panel relative w-full max-w-[480px] !bg-panel p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="label !text-signal/80">{text.eyebrow}</p>
            <h2 className="mt-1 text-[17px] font-medium text-ink">{text.title}</h2>
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
            <p className="text-[12px] text-dim">{text.connected}</p>
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
              {text.setupDoc && (
                <a href={text.setupDoc} target="_blank" rel="noreferrer" className="h-8 rounded-md px-3 font-mono text-[10.5px] uppercase leading-8 tracking-[0.14em] text-dim hover:text-slate-200">
                  Setup guide
                </a>
              )}
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
                <span className="font-mono text-signal/70">2.</span> Enter this code and sign in with {text.account}
              </li>
            </ol>
            <div className="mt-4 flex items-center gap-3 rounded-lg border border-signal/30 bg-black/40 px-4 py-4">
              <code className="min-w-0 flex-1 text-center font-mono text-[40px] leading-none font-semibold tracking-[0.18em] text-ink select-all" aria-live="polite">
                {start?.user_code ?? "········"}
              </code>
              <button
                onClick={copyCode}
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
              {text.footer}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
