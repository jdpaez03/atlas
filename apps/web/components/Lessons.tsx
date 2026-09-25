"use client";

import { useMemo, useState } from "react";
import { apiErrorText, type LessonsApi } from "@/lib/api";
import type { AgentDefinition, Lesson } from "@/lib/contracts";
import { cx } from "@/lib/ui";
import { Empty, Panel, Tag } from "./primitives";

/* Lessons tray (docs/LESSONS.md): comments → proposed rules → approved lessons each agent follows. */

const ALL = "*";
const BTN = "h-8 rounded-md border px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] transition disabled:opacity-40";

function who(id: string, agents: Map<string, AgentDefinition>): { name: string; color: string } {
  if (id === ALL) return { name: "All agents", color: "#e2e8f0" };
  const a = agents.get(id);
  return { name: a?.name ?? id.toUpperCase(), color: a?.color ?? "#94a3b8" };
}

function when(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("es-MX", { day: "numeric", month: "short", year: "numeric" });
}

function Composer({ agents, api, initialAgent }: { agents: AgentDefinition[]; api: LessonsApi; initialAgent: string }) {
  const [agentId, setAgentId] = useState(initialAgent);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<null | "comment" | "direct">(null);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function send(mode: "comment" | "direct") {
    if (!text.trim()) return;
    setBusy(mode);
    setErr(null);
    setDone(null);
    try {
      const out = await api.create(agentId, text, mode);
      setText("");
      setDone(mode === "direct" ? "Rule saved and active." : `${out.length} rule${out.length === 1 ? "" : "s"} proposed — review them on the right.`);
    } catch (x) {
      setErr(apiErrorText(x));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel code="01" title="Teach an agent" className="min-h-0">
      <div className="flex flex-col gap-3 p-4">
        <label className="flex flex-col gap-1.5">
          <span className="label">Agent</span>
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            className="h-9 rounded-md border border-edge bg-black/30 px-2.5 font-mono text-[12px] text-slate-200 focus:border-signal/60 focus:outline-none"
          >
            <option value={ALL}>All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} — {a.title}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="label">Comment</span>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            placeholder="E.g. “In the committee deck the tables are too long and the text is small” · “ORACLE: always include a downside scenario”"
            className="resize-y rounded-md border border-edge bg-black/30 px-3 py-2 text-[13px] leading-relaxed text-ink placeholder:text-mute focus:border-signal/60 focus:outline-none focus:ring-1 focus:ring-signal/30"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send("comment");
            }}
          />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => send("comment")} disabled={!!busy || !text.trim()} className={cx(BTN, "border-signal/50 bg-signal/10 text-signal hover:bg-signal/20")}>
            {busy === "comment" ? "Drafting…" : "Propose rule"}
          </button>
          <button onClick={() => send("direct")} disabled={!!busy || !text.trim()} className={cx(BTN, "border-edge text-dim hover:text-slate-200")} title="Your text is the rule: active right away">
            {busy === "direct" ? "Saving…" : "Save as rule"}
          </button>
        </div>
        <p className="font-mono text-[10px] leading-relaxed text-mute">
          <span className="text-dim">Propose rule</span>: ATLAS turns your comment into 1–3 general rules and waits for your approval.{" "}
          <span className="text-dim">Save as rule</span>: your text becomes an active rule as written. Active lessons are added to the agent&apos;s instructions from its next run.
        </p>
        {done && <p className="font-mono text-[10.5px] text-emerald-300/90">{done}</p>}
        {err && <p className="rounded-md border border-red-500/40 bg-red-500/[0.07] px-3 py-1.5 text-[12px] text-red-200">{err}</p>}
      </div>
    </Panel>
  );
}

function ProposedCard({ l, agents, api, lessons }: { l: Lesson; agents: Map<string, AgentDefinition>; api: LessonsApi; lessons: Lesson[] }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(l.text);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const w = who(l.agent_id, agents);
  const replaced = (l.replaces ?? []).map((id) => lessons.find((x) => x.id === id)).filter((x): x is Lesson => !!x);

  async function act(change: { status?: Lesson["status"]; text?: string }) {
    setBusy(true);
    setErr(null);
    try {
      await api.decide(l.id, change);
      setEditing(false);
    } catch (x) {
      setErr(apiErrorText(x));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-lg border border-amber-400/25 bg-gradient-to-br from-amber-500/[0.06] to-transparent p-3">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Tag color={w.color}>{w.name}</Tag>
        <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-amber-300/80">Proposed</span>
        <span className="ml-auto font-mono text-[9.5px] text-mute">{when(l.created_at)}</span>
      </div>
      {editing ? (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          className="w-full resize-y rounded-md border border-edge bg-black/30 px-2.5 py-1.5 text-[13px] leading-relaxed text-ink focus:border-signal/60 focus:outline-none"
        />
      ) : (
        <p className="text-[13.5px] leading-relaxed text-ink">{l.text}</p>
      )}
      {l.comment && l.comment !== l.text && (
        <p className="mt-1.5 border-l-2 border-edge pl-2 text-[11.5px] italic leading-relaxed text-mute">Your comment: “{l.comment}”</p>
      )}
      {replaced.length > 0 && (
        <p className="mt-1.5 font-mono text-[10px] text-dim">Replaces: {replaced.map((x) => `“${x.text.slice(0, 70)}${x.text.length > 70 ? "…" : ""}”`).join(" · ")}</p>
      )}
      <div className="mt-2.5 flex flex-wrap gap-2">
        {editing ? (
          <>
            <button disabled={busy || !text.trim()} onClick={() => act({ text, status: "active" })} className={cx(BTN, "border-emerald-400/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20")}>
              Save &amp; approve
            </button>
            <button disabled={busy} onClick={() => { setEditing(false); setText(l.text); }} className={cx(BTN, "border-edge text-dim hover:text-slate-200")}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button disabled={busy} onClick={() => act({ status: "active" })} className={cx(BTN, "border-emerald-400/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20")}>
              Approve
            </button>
            <button disabled={busy} onClick={() => setEditing(true)} className={cx(BTN, "border-edge text-dim hover:text-slate-200")}>
              Edit
            </button>
            <button disabled={busy} onClick={() => act({ status: "dismissed" })} className={cx(BTN, "border-edge text-mute hover:text-red-300")}>
              Discard
            </button>
          </>
        )}
      </div>
      {err && <p className="mt-2 text-[12px] text-red-300">{err}</p>}
    </li>
  );
}

function ActiveRow({ l, api }: { l: Lesson; api: LessonsApi }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(l.text);
  const [busy, setBusy] = useState(false);
  async function act(change: { status?: Lesson["status"]; text?: string }) {
    setBusy(true);
    try {
      await api.decide(l.id, change);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="group flex items-start gap-2.5 py-1.5">
      <span className="mt-[8px] h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-300/80" />
      {editing ? (
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} className="w-full resize-y rounded-md border border-edge bg-black/30 px-2.5 py-1.5 text-[13px] text-ink focus:outline-none" />
          <div className="flex gap-2">
            <button disabled={busy || !text.trim()} onClick={() => act({ text })} className={cx(BTN, "h-7 border-signal/50 text-signal")}>
              Save
            </button>
            <button onClick={() => { setEditing(false); setText(l.text); }} className={cx(BTN, "h-7 border-edge text-dim")}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <span className="min-w-0 flex-1 text-[13px] leading-relaxed text-slate-200">{l.text}</span>
          <span className="flex shrink-0 gap-2 font-mono text-[10px] opacity-0 transition group-hover:opacity-100">
            <button onClick={() => setEditing(true)} className="text-dim hover:text-slate-200">edit</button>
            <button disabled={busy} onClick={() => act({ status: "retired" })} className="text-mute hover:text-red-300">retire</button>
          </span>
        </>
      )}
    </li>
  );
}

export function LessonsView({ lessons, agents, api, ready }: { lessons: Lesson[]; agents: AgentDefinition[]; api: LessonsApi; ready: boolean }) {
  const byId = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const [showPast, setShowPast] = useState(false);
  const newest = (a: Lesson, b: Lesson) => Date.parse(b.created_at) - Date.parse(a.created_at);
  const proposed = lessons.filter((l) => l.status === "proposed").sort(newest);
  const active = lessons.filter((l) => l.status === "active");
  const past = lessons.filter((l) => l.status === "dismissed" || l.status === "retired").sort(newest);
  const groups = useMemo(() => {
    const m = new Map<string, Lesson[]>();
    for (const l of active) m.set(l.agent_id, [...(m.get(l.agent_id) ?? []), l]);
    const order = [ALL, ...agents.map((a) => a.id)];
    return [...m.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }, [active, agents]);

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
      <div className="flex flex-col gap-4">
        {ready ? <Composer agents={agents} api={api} initialAgent="scribe" /> : <Panel code="01" title="Teach an agent"><Empty>Connecting…</Empty></Panel>}
      </div>
      <div className="flex min-w-0 flex-col gap-4">
        <Panel code="02" title="Waiting for your approval" meta={<span className="font-mono text-[10px] text-amber-300/80">{proposed.length}</span>}>
          {proposed.length === 0 ? (
            <Empty>No proposals. Write a comment on the left and ATLAS will turn it into rules for you to approve.</Empty>
          ) : (
            <ul className="grid gap-2.5 p-4">
              {proposed.map((l) => (
                <ProposedCard key={l.id} l={l} agents={byId} api={api} lessons={lessons} />
              ))}
            </ul>
          )}
        </Panel>
        <Panel code="03" title="Active lessons" meta={<span className="font-mono text-[10px] text-emerald-300/80">{active.length}</span>}>
          {groups.length === 0 ? (
            <Empty>No active lessons yet.</Empty>
          ) : (
            <div className="grid gap-4 p-4 md:grid-cols-2">
              {groups.map(([agentId, items]) => {
                const w = who(agentId, byId);
                return (
                  <section key={agentId} className="rounded-lg border border-edge/70 bg-black/15 p-3">
                    <h4 className="mb-1 flex items-center gap-2 font-mono text-[10.5px] font-semibold uppercase tracking-[0.18em]" style={{ color: w.color }}>
                      {w.name}
                      <span className="font-normal text-mute">· {items.length}</span>
                    </h4>
                    <ul className="divide-y divide-edge/40">
                      {items.map((l) => (
                        <ActiveRow key={l.id} l={l} api={api} />
                      ))}
                    </ul>
                  </section>
                );
              })}
            </div>
          )}
        </Panel>
        {past.length > 0 && (
          <div className="px-1">
            <button onClick={() => setShowPast((v) => !v)} className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute hover:text-slate-300">
              {showPast ? "Hide" : "Show"} discarded &amp; retired · {past.length}
            </button>
            {showPast && (
              <ul className="mt-2 grid gap-1">
                {past.map((l) => {
                  const w = who(l.agent_id, byId);
                  return (
                    <li key={l.id} className="flex items-start gap-2 text-[12px] text-mute">
                      <Tag color={w.color}>{w.name}</Tag>
                      <span className="min-w-0 flex-1 line-through decoration-slate-600">{l.text}</span>
                      <span className="shrink-0 font-mono text-[9.5px] uppercase">{l.status}</span>
                      <button onClick={() => api.decide(l.id, { status: "active" })} className="shrink-0 font-mono text-[10px] text-dim hover:text-emerald-300">
                        restore
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
