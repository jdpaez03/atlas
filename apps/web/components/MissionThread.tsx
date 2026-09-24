"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentDefinition, AgentMessage, Mission, Task } from "@/lib/contracts";
import { MSG, cx, hms, msgFrom, msgTo } from "@/lib/ui";
import { DropZone, FileLink, PaperclipIcon, acceptFiles } from "./Files";
import { Empty, Panel } from "./primitives";

const HUMAN = "human";

type Item =
  | { kind: "msg"; at: string; m: AgentMessage }
  | { kind: "round"; at: string; round: number };

/**
 * Mission thread (docs/PHASE3.md §B/§C): the human ↔ ATLAS conversation inside a mission.
 * Built from state.messages where from/to is "human", with round dividers from tasks' `round`.
 */
export function MissionThread({
  mission,
  messages,
  tasks,
  agents,
  send,
  attach,
  className,
}: {
  mission: Mission | null;
  messages: AgentMessage[];
  tasks: Task[];
  agents: Map<string, AgentDefinition>;
  send: (missionId: string, text: string) => Promise<unknown>;
  attach: (missionId: string, files: File[]) => Promise<unknown>;
  className?: string;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showFiles, setShowFiles] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const mid = mission?.id ?? null;
  useEffect(() => {
    setText("");
    setErr(null);
    setShowFiles(false);
  }, [mid]);

  const items = useMemo<Item[]>(() => {
    const out: Item[] = messages.filter((m) => msgFrom(m) === HUMAN || msgTo(m) === HUMAN).map((m) => ({ kind: "msg", at: m.created_at, m }));
    // A round divider at the first task of each follow-up round.
    const firstOfRound = new Map<number, string>();
    for (const t of tasks) {
      const r = t.round ?? 1;
      if (r < 2) continue;
      const cur = firstOfRound.get(r);
      if (!cur || t.created_at < cur) firstOfRound.set(r, t.created_at);
    }
    // The mission may already be in round N before its first task is created.
    const round = mission?.round ?? 1;
    if (round > 1 && !firstOfRound.has(round)) {
      const lastHuman = [...out].reverse().find((x) => x.kind === "msg" && msgFrom(x.m) === HUMAN);
      firstOfRound.set(round, lastHuman ? new Date(new Date(lastHuman.at).getTime() + 1).toISOString() : new Date().toISOString());
    }
    for (const [r, at] of firstOfRound) out.push({ kind: "round", at, round: r });
    return out.sort((a, b) => a.at.localeCompare(b.at));
  }, [messages, tasks, mission?.round]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length, mid]);

  const live = mission?.mode === "live";
  const closed = mission?.phase === "CLOSED";
  const interrupted = !!mission?.interrupted && !closed;
  const canWrite = !!mission && live;
  const last = items[items.length - 1];
  const awaiting = canWrite && last?.kind === "msg" && msgFrom(last.m) === HUMAN;

  async function submit() {
    const body = text.trim();
    if (!mission || !body || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await send(mission.id, body);
      setText("");
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Send failed");
    } finally {
      setBusy(false);
    }
  }

  async function upload(list: File[]) {
    if (!mission || !list.length) return;
    const { accepted, errors } = acceptFiles(list, [], mission.attachments?.length ?? 0);
    if (errors.length) setErr(errors.join(" · "));
    if (!accepted.length) return;
    setUploading(true);
    try {
      await attach(mission.id, accepted);
      setShowFiles(true);
      if (!errors.length) setErr(null);
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const attachments = mission?.attachments ?? [];
  const placeholder = !mission
    ? "No mission selected"
    : interrupted
      ? "Send a message to resume this mission…"
      : closed
        ? "Ask a follow-up — ATLAS may open a new round…"
        : "Guide the team — every task that starts next gets this…";

  return (
    <Panel
      code="08"
      title="Mission thread"
      className={className}
      bodyClassName="flex min-h-0 flex-col"
      meta={
        mission && (
          <button
            type="button"
            onClick={() => setShowFiles((v) => !v)}
            className={cx("flex items-center gap-1 rounded px-1.5 py-0.5 transition hover:text-slate-200", showFiles && "bg-white/[0.05] text-slate-200")}
            title="Mission attachments"
            aria-expanded={showFiles}
          >
            <PaperclipIcon size={11} />
            {attachments.length}
          </button>
        )
      }
    >
      {showFiles && mission && (
        <div className="border-b border-edge/60 bg-black/20 px-3 py-2">
          {attachments.length > 0 ? (
            <ul className="grid max-h-28 gap-1 overflow-y-auto scroll-thin">
              {attachments.map((a) => (
                <li key={a.id}>
                  <FileLink a={a} compact />
                </li>
              ))}
            </ul>
          ) : (
            <p className="font-mono text-[10px] text-mute">No files attached to this mission.</p>
          )}
          {canWrite && (
            <DropZone onFiles={upload} disabled={uploading} className="mt-2 !py-1.5" hint={uploading ? "uploading…" : "25 MB each"} />
          )}
        </div>
      )}

      <div
        ref={scroller}
        className={cx("relative min-h-0 flex-1 overflow-y-auto px-3 py-3 scroll-thin", dragOver && "bg-signal/[0.05]")}
        onDragOver={(e) => {
          if (!canWrite) return;
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          if (!canWrite) return;
          e.preventDefault();
          setDragOver(false);
          void upload([...e.dataTransfer.files]);
        }}
      >
        {!mission ? (
          <Empty>Talk to ATLAS once a mission is running.</Empty>
        ) : items.length === 0 ? (
          interrupted ? null : <Empty className="!min-h-16 !py-4">
            {live
              ? closed
                ? "Mission closed. Ask ATLAS a follow-up question or request more work."
                : "Add guidance or context for the team while they work."
              : "No thread messages."}
          </Empty>
        ) : (
          <ol className="flex flex-col gap-2.5">
            {items.map((it) =>
              it.kind === "round" ? (
                <li key={`round-${it.round}`} className="flex items-center gap-2 py-1 font-mono text-[9px] uppercase tracking-[0.22em] text-violet-300/80">
                  <span className="h-px flex-1 bg-violet-400/25" />
                  Round {it.round} started · {hms(it.at)}
                  <span className="h-px flex-1 bg-violet-400/25" />
                </li>
              ) : (
                <Bubble key={it.m.id} m={it.m} agents={agents} />
              ),
            )}
            {awaiting && (
              <li className="flex items-center gap-2 pl-1 font-mono text-[10px] text-dim">
                <span className="flex gap-0.5">
                  {[0, 1, 2].map((i) => (
                    <span key={i} className="h-1 w-1 animate-pulse rounded-full bg-slate-400" style={{ animationDelay: `${i * 180}ms` }} />
                  ))}
                </span>
                ATLAS is reading your note
              </li>
            )}
          </ol>
        )}
        {interrupted && (
          <p className="mt-3 rounded border border-red-400/30 bg-red-500/[0.06] px-2.5 py-1.5 font-mono text-[10px] leading-relaxed text-red-200/90">
            The server stopped while this mission was running. Open tasks were cancelled — send a message to resume.
          </p>
        )}
      </div>

      <div className="border-t border-edge/70 bg-black/20 p-2.5">
        {mission && !live ? (
          <p className="px-1 py-1.5 font-mono text-[10px] leading-relaxed text-mute">
            <span className="text-slate-400">Simulated mission</span> — the thread and follow-ups need a live mission.
          </p>
        ) : (
          <>
            <div className="flex items-end gap-1.5 rounded-md border border-edge bg-black/30 focus-within:border-signal/60 focus-within:ring-1 focus-within:ring-signal/30">
              <button
                type="button"
                disabled={!canWrite || uploading}
                onClick={() => fileInput.current?.click()}
                className="mb-1 ml-1 flex h-7 w-7 shrink-0 items-center justify-center rounded text-dim transition hover:bg-white/[0.05] hover:text-slate-200 disabled:opacity-40"
                title="Attach files to this mission"
                aria-label="Attach files"
              >
                <PaperclipIcon />
              </button>
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  const list = e.target.files ? [...e.target.files] : [];
                  e.target.value = "";
                  void upload(list);
                }}
              />
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                disabled={!canWrite}
                rows={2}
                placeholder={placeholder}
                aria-label="Message ATLAS"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void submit();
                  }
                }}
                className="max-h-32 min-h-[40px] flex-1 resize-none bg-transparent py-2 text-[12.5px] leading-snug text-ink placeholder:text-mute focus:outline-none disabled:cursor-not-allowed"
              />
              <button
                type="button"
                onClick={() => void submit()}
                disabled={!canWrite || busy || !text.trim()}
                className="mr-1 mb-1 flex h-7 shrink-0 items-center gap-1 rounded border border-signal/50 bg-signal/10 px-2 font-mono text-[9.5px] font-semibold uppercase tracking-[0.18em] text-signal transition hover:bg-signal/20 disabled:border-edge-2 disabled:bg-transparent disabled:text-mute"
              >
                {busy ? "…" : "Send"}
              </button>
            </div>
            <p className="mt-1 flex justify-between gap-2 px-0.5 font-mono text-[9px] tracking-[0.06em] text-mute">
              <span>{uploading ? "Uploading…" : "Enter to send"}</span>
              <span>Shift+Enter newline</span>
            </p>
          </>
        )}
        {err && <p className="mt-1 px-0.5 font-mono text-[10px] text-red-400">{err}</p>}
      </div>
    </Panel>
  );
}

function Bubble({ m, agents }: { m: AgentMessage; agents: Map<string, AgentDefinition> }) {
  const from = msgFrom(m);
  const mine = from === HUMAN;
  const a = agents.get(from);
  const color = a?.color ?? "#e2e8f0";
  const t = MSG[m.type] ?? MSG.ANSWER;
  return (
    <li className={cx("flex flex-col", mine ? "items-end" : "items-start")}>
      <span className="mb-0.5 flex items-center gap-1.5 font-mono text-[8.5px] uppercase tracking-[0.18em]">
        {mine ? (
          <span className="text-signal/80">You</span>
        ) : (
          <>
            <span className="font-semibold" style={{ color }}>
              {a?.name ?? from.toUpperCase()}
            </span>
            {m.type !== "ANSWER" && <span style={{ color: t.color }}>{t.label}</span>}
          </>
        )}
        <span className="text-mute tracking-normal">{hms(m.created_at)}</span>
      </span>
      <div
        className={cx(
          "max-w-[92%] whitespace-pre-wrap break-words rounded-lg border px-2.5 py-1.5 text-[12px] leading-snug",
          mine ? "rounded-tr-sm border-signal/30 bg-signal/[0.09] text-slate-100" : "rounded-tl-sm text-slate-200",
        )}
        style={mine ? undefined : { borderColor: `${color}30`, background: `${color}0a` }}
      >
        {m.body || m.subject}
        {m.attachments?.length > 0 && (
          <div className="mt-1.5 grid gap-1">
            {m.attachments.map((x) => (
              <FileLink key={x.id} a={x} compact />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}
