"use client";

import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { apiErrorText, type MemoryApi } from "@/lib/api";
import type { AskMessage, KnowledgeNote, MemoryRef, Mission } from "@/lib/contracts";
import { cx } from "@/lib/ui";
import { Empty, Panel } from "./primitives";

/* Ask ATLAS (docs/MEMORY.md): talk with the orchestrator about what happened, with memory of earlier work. */

const BTN = "h-8 rounded-md border px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] transition disabled:opacity-40";
const KIND: Record<MemoryRef["kind"], { label: string; color: string }> = {
  mission: { label: "Mission", color: "#7dd3fc" },
  brief: { label: "L10 brief", color: "#fbbf24" },
  digest: { label: "CC digest", color: "#c4b5fd" },
};

function day(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString("es-MX", { day: "numeric", month: "short" }) : "";
}

/** Tiny markdown: paragraphs, "- " bullets, **bold**. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i} className="font-semibold text-slate-100">{part.slice(2, -2)}</strong> : <Fragment key={i}>{part}</Fragment>,
  );
}

function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="grid gap-2">
      {blocks.map((b, i) => {
        const lines = b.split("\n").filter((l) => l.trim());
        if (lines.length && lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
          return (
            <ul key={i} className="grid gap-1">
              {lines.map((l, j) => (
                <li key={j} className="flex gap-2">
                  <span className="mt-[8px] h-1 w-1 shrink-0 rounded-full bg-signal/70" />
                  <span className="min-w-0">{inline(l.replace(/^\s*[-*•]\s+/, ""))}</span>
                </li>
              ))}
            </ul>
          );
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function RefChip({ r, onOpen }: { r: MemoryRef; onOpen: (r: MemoryRef) => void }) {
  const k = KIND[r.kind];
  return (
    <button
      onClick={() => onOpen(r)}
      className="flex max-w-full items-center gap-1.5 rounded border px-1.5 py-[2px] font-mono text-[10px] text-slate-300 transition hover:bg-white/[0.05]"
      style={{ borderColor: `${k.color}40` }}
      title={r.title}
    >
      <span className="shrink-0 whitespace-nowrap" style={{ color: k.color }}>{k.label}</span>
      <span className="shrink-0 whitespace-nowrap text-mute">{day(r.date)}</span>
      <span className="truncate">{r.title}</span>
    </button>
  );
}

function AtlasTurn({
  m,
  node,
  api,
  onOpenRef,
  onLaunch,
  onNoteSaved,
}: {
  m: AskMessage;
  node: string;
  api: MemoryApi;
  onOpenRef: (r: MemoryRef) => void;
  onLaunch: (objective: string) => Promise<void>;
  onNoteSaved: () => void;
}) {
  const [saved, setSaved] = useState<Record<string, "saved" | "dismissed">>({});
  const [launching, setLaunching] = useState(false);
  const [launched, setLaunched] = useState(false);
  return (
    <div className="flex max-w-[92%] flex-col gap-2 self-start">
      <div className="rounded-lg rounded-tl-sm border border-signal/20 bg-signal/[0.04] px-3.5 py-2.5 text-[13.5px] leading-relaxed text-slate-200">
        <Markdown text={m.text} />
      </div>
      {m.refs.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {m.refs.map((r) => (
            <RefChip key={r.id} r={r} onOpen={onOpenRef} />
          ))}
        </div>
      )}
      {m.remember.filter((f) => !saved[f] || saved[f] === "saved").map((f) => (
        <div key={f} className="flex flex-wrap items-center gap-2 rounded-md border border-emerald-400/25 bg-emerald-500/[0.05] px-3 py-2 text-[12.5px] text-slate-200">
          <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-emerald-300/90">Remember?</span>
          <span className="min-w-0 flex-1">{f}</span>
          {saved[f] === "saved" ? (
            <span className="font-mono text-[10px] text-emerald-300">Saved ✓</span>
          ) : (
            <span className="flex gap-2">
              <button
                onClick={async () => {
                  await api.addNote(node, f, "ask");
                  setSaved((s) => ({ ...s, [f]: "saved" }));
                  onNoteSaved();
                }}
                className="font-mono text-[10px] uppercase tracking-[0.14em] text-emerald-300 hover:text-emerald-200"
              >
                Save
              </button>
              <button onClick={() => setSaved((s) => ({ ...s, [f]: "dismissed" }))} className="font-mono text-[10px] uppercase tracking-[0.14em] text-mute hover:text-slate-300">
                No
              </button>
            </span>
          )}
        </div>
      ))}
      {m.suggest_mission && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-400/25 bg-amber-500/[0.05] px-3 py-2 text-[12.5px] text-slate-200">
          <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-amber-300/90">Needs new work</span>
          <span className="min-w-0 flex-1">{m.suggest_mission}</span>
          <button
            disabled={launching || launched}
            onClick={async () => {
              setLaunching(true);
              try {
                await onLaunch(m.suggest_mission!);
                setLaunched(true);
              } finally {
                setLaunching(false);
              }
            }}
            className={cx(BTN, "h-7 border-emerald-400/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20")}
          >
            {launched ? "Launched" : launching ? "Launching…" : "Launch mission"}
          </button>
        </div>
      )}
    </div>
  );
}

function Notes({ node, api, refreshKey }: { node: string; api: MemoryApi; refreshKey: number }) {
  const [notes, setNotes] = useState<KnowledgeNote[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    api.notes(node).then(setNotes).catch(() => setNotes([]));
  }, [api, node]);
  useEffect(() => {
    load();
  }, [load, refreshKey]);
  return (
    <Panel code="02" title="What ATLAS knows from you" meta={<span className="font-mono text-[10px] text-emerald-300/80">{notes.length}</span>}>
      <div className="flex flex-col gap-2 p-3">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          placeholder="A fact to keep: “Josué lleva Balcones desde septiembre”"
          className="resize-y rounded-md border border-edge bg-black/30 px-2.5 py-1.5 text-[12.5px] text-ink placeholder:text-mute focus:border-signal/60 focus:outline-none"
        />
        <button
          disabled={busy || !text.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await api.addNote(node, text);
              setText("");
              load();
            } finally {
              setBusy(false);
            }
          }}
          className={cx(BTN, "self-start border-emerald-400/50 text-emerald-300 hover:bg-emerald-500/10")}
        >
          Remember
        </button>
        {notes.length === 0 ? (
          <p className="font-mono text-[10.5px] text-mute">No notes yet. Every note is given to ATLAS and the agents in each run.</p>
        ) : (
          <ul className="grid gap-1.5">
            {notes.map((n) => (
              <li key={n.id} className="group flex items-start gap-2 text-[12.5px] leading-relaxed text-slate-300">
                <span className="mt-[2px] shrink-0 font-mono text-[9.5px] text-mute">{day(n.created_at)}</span>
                <span className="min-w-0 flex-1">{n.text}</span>
                <button onClick={() => api.updateNote(n.id, { status: "retired" }).then(load)} className="shrink-0 font-mono text-[10px] text-mute opacity-0 hover:text-red-300 group-hover:opacity-100">
                  forget
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function MemoryIndex({ node, api, onOpenRef, refreshKey }: { node: string; api: MemoryApi; onOpenRef: (r: MemoryRef) => void; refreshKey: string }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<MemoryRef[] | null>(null);
  useEffect(() => {
    const t = setTimeout(() => {
      api.index(node, q.trim() || undefined).then(setItems).catch(() => setItems([]));
    }, 250);
    return () => clearTimeout(t);
  }, [api, node, q, refreshKey]);
  return (
    <Panel code="03" title="Memory" meta={<span className="font-mono text-[10px] text-mute">{items?.length ?? "…"}</span>}>
      <div className="flex flex-col gap-2 p-3">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search earlier missions, briefs, digests…"
          className="h-8 rounded-md border border-edge bg-black/30 px-2.5 font-mono text-[11.5px] text-slate-200 placeholder:text-mute focus:border-signal/60 focus:outline-none"
        />
        {items && items.length === 0 ? (
          <p className="font-mono text-[10.5px] text-mute">{q ? "Nothing matches." : "Nothing yet: every closed mission, brief and digest lands here."}</p>
        ) : (
          <ul className="grid max-h-[420px] gap-1 overflow-y-auto">
            {(items ?? []).map((r) => (
              <li key={r.id}>
                <RefChip r={r} onOpen={onOpenRef} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

export function AskView({
  node,
  nodeName,
  api,
  ready,
  launch,
  onOpenRef,
  refreshKey,
}: {
  node: string;
  nodeName: string;
  api: MemoryApi;
  ready: boolean;
  launch: (objective: string) => Promise<Mission>;
  onOpenRef: (r: MemoryRef) => void;
  refreshKey: string;
}) {
  const [thread, setThread] = useState<AskMessage[] | null>(null);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [notesKey, setNotesKey] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ready) return;
    api.thread(node).then(setThread).catch(() => setThread([]));
  }, [api, node, ready]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [thread, pending]);

  async function send() {
    const q = text.trim();
    if (!q || pending) return;
    setPending(q);
    setText("");
    setErr(null);
    try {
      const out = await api.ask(node, q);
      setThread((t) => [...(t ?? []), ...out]);
    } catch (x) {
      setErr(apiErrorText(x));
      setText(q);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
      <Panel
        code="01"
        title={`Ask ATLAS · ${nodeName}`}
        meta={
          <button
            onClick={async () => {
              await api.clear(node);
              setThread([]);
            }}
            className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute hover:text-slate-200"
          >
            New conversation
          </button>
        }
        bodyClassName="flex flex-col"
      >
        <div className="flex min-h-[420px] flex-1 flex-col gap-4 overflow-y-auto p-4 lg:max-h-[calc(100vh-280px)]">
          {thread === null ? (
            <Empty>Loading…</Empty>
          ) : thread.length === 0 && !pending ? (
            <Empty>
              Ask about anything done here — “¿qué concluimos de Balcones?”, “¿qué pendientes tiene Josué?”, “¿qué pasó con el crédito puente?”. ATLAS answers from earlier
              missions, briefs, digests, your notes and the live state, and tells you when it needs new work.
            </Empty>
          ) : (
            thread.map((m) =>
              m.role === "human" ? (
                <div key={m.id} className="max-w-[80%] self-end rounded-lg rounded-tr-sm border border-edge bg-white/[0.04] px-3.5 py-2 text-[13.5px] leading-relaxed text-ink">
                  {m.text}
                </div>
              ) : (
                <AtlasTurn
                  key={m.id}
                  m={m}
                  node={node}
                  api={api}
                  onOpenRef={onOpenRef}
                  onNoteSaved={() => setNotesKey((k) => k + 1)}
                  onLaunch={async (objective) => {
                    const mission = await launch(objective);
                    onOpenRef({ id: mission.id, kind: "mission", title: mission.objective, date: mission.created_at });
                  }}
                />
              ),
            )
          )}
          {pending && (
            <>
              <div className="max-w-[80%] self-end rounded-lg rounded-tr-sm border border-edge bg-white/[0.04] px-3.5 py-2 text-[13.5px] text-ink">{pending}</div>
              <div className="self-start font-mono text-[11px] text-signal/80">ATLAS is thinking…</div>
            </>
          )}
          <div ref={endRef} />
        </div>
        <div className="border-t border-edge/60 p-3">
          {err && <p className="mb-2 rounded-md border border-red-500/40 bg-red-500/[0.07] px-3 py-1.5 text-[12px] text-red-200">{err}</p>}
          <div className="flex items-end gap-2">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={2}
              disabled={!ready}
              placeholder="Ask, tell ATLAS what happened, or follow up on something…"
              className="min-h-[44px] flex-1 resize-y rounded-md border border-edge bg-black/30 px-3 py-2 text-[13px] leading-relaxed text-ink placeholder:text-mute focus:border-signal/60 focus:outline-none focus:ring-1 focus:ring-signal/30"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button onClick={send} disabled={!ready || !!pending || !text.trim()} className={cx(BTN, "h-11 border-signal/50 bg-signal/10 text-signal hover:bg-signal/20")}>
              Send
            </button>
          </div>
        </div>
      </Panel>
      <div className="flex min-w-0 flex-col gap-4">
        <Notes node={node} api={api} refreshKey={notesKey} />
        <MemoryIndex node={node} api={api} onOpenRef={onOpenRef} refreshKey={refreshKey} />
      </div>
    </div>
  );
}
