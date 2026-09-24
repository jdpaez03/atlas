"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { apiErrorText } from "@/lib/api";
import type { AtlasEvent, Digest, DigestThread, FollowUp, Mission, MissionReport } from "@/lib/contracts";
import { isOpen, parseAddr, shortDate } from "@/lib/followups";
import { PRIORITY, cx } from "@/lib/ui";
import { Tag } from "./primitives";

export type FollowUpsTab = "board" | "digest";

/* ------------------------------------------------------------------------------------------------ sub-switch */

export function FollowUpsTabs({ tab, onTab, unread }: { tab: FollowUpsTab; onTab: (t: FollowUpsTab) => void; unread: boolean }) {
  const tabs: { id: FollowUpsTab; label: string }[] = [
    { id: "board", label: "Board" },
    { id: "digest", label: "Digest" },
  ];
  return (
    <div role="tablist" aria-label="Follow-ups view" className="flex shrink-0 items-center rounded-md border border-edge bg-black/30 p-0.5">
      {tabs.map((t) => {
        const active = tab === t.id;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={active}
            onClick={() => onTab(t.id)}
            className={cx(
              "relative flex h-6 items-center gap-1.5 rounded px-2.5 font-mono text-[10px] uppercase tracking-[0.16em] transition",
              active ? "bg-signal/[0.12] text-ink shadow-[inset_0_0_0_1px_rgba(125,211,252,0.3)]" : "text-dim hover:text-slate-200",
            )}
          >
            {t.label}
            {t.id === "digest" && unread && !active && (
              <span className="h-1.5 w-1.5 rounded-full bg-violet-300 dot-live" style={{ ["--c" as string]: "#c4b5fdaa" }} aria-label="new digest" />
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ helpers */

const RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
const PROJECT_COLORS = ["#7dd3fc", "#fbbf24", "#34d399", "#f0abfc", "#fb923c", "#a78bfa", "#2dd4bf"];
function projectColor(p: string): string {
  let h = 0;
  for (const c of p) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PROJECT_COLORS[h % PROJECT_COLORS.length];
}

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("es-MX", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

function groupThreads(threads: DigestThread[]): { project: string | null; threads: DigestThread[] }[] {
  const by = new Map<string | null, DigestThread[]>();
  for (const t of threads) {
    const k = t.project?.trim() || null;
    by.set(k, [...(by.get(k) ?? []), t]);
  }
  const groups = [...by.entries()].map(([project, list]) => ({
    project,
    threads: [...list].sort((a, b) => (RANK[a.importance] ?? 9) - (RANK[b.importance] ?? 9)),
  }));
  const best = (g: { threads: DigestThread[] }) => Math.min(...g.threads.map((t) => RANK[t.importance] ?? 9));
  return groups.sort((a, b) => {
    if (!a.project !== !b.project) return a.project ? -1 : 1; // untagged last
    return best(a) - best(b) || b.threads.length - a.threads.length || (a.project ?? "").localeCompare(b.project ?? "");
  });
}

function participantsText(list: string[]): { short: string; full: string } {
  const names = list.map((p) => parseAddr(p).name);
  const short = names.length <= 2 ? names.join(", ") : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
  return { short, full: list.join("\n") };
}

/* ------------------------------------------------------------------------------------------------ thread card */

function Section({ label, color, children }: { label: string; color: string; children: ReactNode }) {
  return (
    <div className="rounded-md border px-2.5 py-2" style={{ borderColor: `${color}30`, background: `${color}08` }}>
      <p className="font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color }}>
        {label}
      </p>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function ThreadCard({ t, followup, onAsk }: { t: DigestThread; followup: FollowUp | undefined; onAsk: (id: string) => void }) {
  const imp = PRIORITY[t.importance] ?? PRIORITY.MEDIUM;
  const who = participantsText(t.participants);
  return (
    <article className="flex min-w-0 flex-col gap-2.5 rounded-lg border border-edge/80 bg-black/20 p-3.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Tag color={imp.color} className="!text-[8.5px]">
          {imp.label}
        </Tag>
        {t.project && (
          <Tag color={projectColor(t.project)} className="!text-[8.5px] !normal-case !tracking-[0.04em]">
            {t.project}
          </Tag>
        )}
        {t.asks_me && (
          <span className="ml-auto inline-flex items-center gap-1 rounded border border-amber-400/50 bg-amber-500/15 px-1.5 py-[1px] font-mono text-[9px] uppercase tracking-[0.16em] text-amber-200">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-300" />
            Asks you
          </span>
        )}
      </div>

      <div>
        <h4 className="text-[14px] leading-snug font-medium text-ink">{t.subject}</h4>
        <p className="mt-0.5 truncate text-[11px] text-dim" title={who.full}>
          {who.short}
        </p>
      </div>

      {t.asks_me &&
        (t.followup_id ? (
          <button
            onClick={() => onAsk(t.followup_id!)}
            className="group flex w-full items-start gap-2 rounded-md border border-amber-400/45 bg-amber-500/[0.08] px-2.5 py-2 text-left transition hover:bg-amber-500/[0.14]"
            title="Show the follow-up on the Board"
          >
            <span className="min-w-0 flex-1 text-[12px] leading-snug text-amber-100">{t.asks_me}</span>
            <span className="shrink-0 pt-[1px] font-mono text-[9px] uppercase tracking-[0.14em] whitespace-nowrap text-amber-300 opacity-80 group-hover:opacity-100">
              {followup && !isOpen(followup) ? `${followup.status.toLowerCase()} ·` : ""} Follow-up →
            </span>
          </button>
        ) : (
          <p className="rounded-md border border-amber-400/45 bg-amber-500/[0.08] px-2.5 py-2 text-[12px] leading-snug text-amber-100">{t.asks_me}</p>
        ))}

      <ul className="grid gap-1 pl-0.5">
        {t.summary.map((s, i) => (
          <li key={i} className="flex gap-2 text-[12.5px] leading-relaxed text-slate-300">
            <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-slate-500" />
            <span className="min-w-0">{s}</span>
          </li>
        ))}
      </ul>

      {(t.decisions.length > 0 || t.figures.length > 0) && (
        <div className="grid gap-2">
          {t.decisions.length > 0 && (
            <Section label="Decisions" color="#34d399">
              <ul className="grid gap-0.5">
                {t.decisions.map((d, i) => (
                  <li key={i} className="flex gap-1.5 text-[11.5px] leading-snug text-slate-200">
                    <span className="text-emerald-400">✓</span>
                    <span className="min-w-0">{d}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
          {t.figures.length > 0 && (
            <Section label="Figures" color="#7dd3fc">
              <ul className="flex flex-wrap gap-1">
                {t.figures.map((f, i) => (
                  <li key={i} className="rounded border border-signal/25 bg-black/30 px-1.5 py-[2px] font-mono text-[10.5px] text-sky-100">
                    {f}
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </div>
      )}

      {t.messages.length > 0 && (
        <div className="mt-auto border-t border-edge/60 pt-2">
          <p className="label !text-[8.5px]">Source emails · {t.messages.length}</p>
          <ul className="mt-1 grid gap-0.5">
            {t.messages.map((m) => (
              <li key={m.message_id} className="group/src relative flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-white/[0.03]">
                <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-dim" aria-hidden>
                  <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.25" />
                  <path d="M2.25 4l5.75 4.5L13.75 4" />
                </svg>
                <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400" tabIndex={m.excerpt ? 0 : undefined} title={m.subject}>
                  <span className="text-slate-300">{parseAddr(m.sender).name}</span>
                  <span className="text-mute"> · {shortDate(m.received_at)}</span>
                </span>
                {m.web_link && (
                  <a
                    href={m.web_link}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex shrink-0 items-center gap-1 rounded px-1 font-mono text-[9px] uppercase tracking-[0.12em] text-signal/80 hover:text-signal"
                    title="Open in Outlook"
                  >
                    Outlook
                    <svg width="9" height="9" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                      <path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M12 9.5v3.75H2.75V4H6.5" />
                    </svg>
                  </a>
                )}
                {/* The engine stores no excerpt for CC emails; show one only if a source provides it. */}
                {m.excerpt && (
                  <div
                    role="tooltip"
                    className="pointer-events-none invisible absolute top-full right-0 left-0 z-20 mt-1 rounded-md border border-edge-2 bg-panel-2 px-3 py-2 text-[11.5px] leading-relaxed text-slate-300 opacity-0 shadow-[0_18px_40px_-12px_rgba(0,0,0,0.9)] transition-opacity group-focus-within/src:visible group-focus-within/src:opacity-100 group-hover/src:visible group-hover/src:opacity-100"
                  >
                    “{m.excerpt}”
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}

/* ------------------------------------------------------------------------------------------------ run a digest */

export const DIGEST_RUN_DAYS = [1, 3, 7] as const;

export interface DigestRun {
  days: number;
  setDays: (d: number) => void;
  running: boolean;
  error: string | null;
  /** why the last run produced no digest (from its mission report or last log line) */
  outcome: string | null;
  start: () => void;
}

/**
 * POST /digests/run {days} → a background mission. It's finished when that mission is CLOSED; a digest that
 * appeared since the run started means success, otherwise the mission's own explanation is shown.
 * Lives in the page so a run survives switching tabs.
 */
export function useDigestRun({
  run,
  digests,
  missions,
  reports,
  feed,
}: {
  run: (days: number) => Promise<Mission>;
  digests: Digest[];
  missions: Mission[];
  reports: MissionReport[];
  feed: AtlasEvent[];
}): DigestRun & { finishedWithDigest: number } {
  const [days, setDays] = useState<number>(3);
  const [missionId, setMissionId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [finishedWithDigest, setFinishedWithDigest] = useState(0);
  const baseline = useRef<Set<string>>(new Set());

  const start = useCallback(() => {
    setStarting(true);
    setError(null);
    setOutcome(null);
    baseline.current = new Set(digests.map((d) => d.id));
    run(days)
      .then((m) => setMissionId(m.id))
      .catch((x) => setError(apiErrorText(x, "Could not start the digest")))
      .finally(() => setStarting(false));
  }, [run, days, digests]);

  const mission = missionId ? missions.find((m) => m.id === missionId) : undefined;
  const closed = !!mission && (mission.phase === "CLOSED" || mission.interrupted);
  useEffect(() => {
    if (!missionId || !closed) return;
    const produced = digests.some((d) => d.mission_id === missionId || !baseline.current.has(d.id));
    if (produced) {
      setOutcome(null);
      setFinishedWithDigest((n) => n + 1);
    } else {
      const report = reports.filter((r) => r.mission_id === missionId).sort((a, b) => b.version - a.version)[0];
      const lastLog = feed.find((e) => e.mission_id === missionId && e.type === "log");
      setOutcome(report?.executive_summary || lastLog?.summary || "The digest run finished without new CC emails to summarize.");
    }
    setMissionId(null);
  }, [missionId, closed, digests, reports, feed]);

  return { days, setDays, running: starting || !!missionId, error, outcome, start, finishedWithDigest };
}

export function DigestRunControl({ run, prominent }: { run: DigestRun; prominent?: boolean }) {
  if (run.running) {
    return (
      <span className={cx("inline-flex items-center gap-2 font-mono text-violet-200", prominent ? "text-[12px]" : "text-[10.5px]")} role="status">
        <svg width={prominent ? 14 : 12} height={prominent ? 14 : 12} viewBox="0 0 16 16" className="animate-spin" aria-hidden>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
          <path d="M14 8a6 6 0 00-6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
        HERMES is reading your CC emails…
      </span>
    );
  }
  return (
    <div className={cx("flex flex-wrap items-center gap-2", prominent && "justify-center")}>
      <span className={cx("font-mono uppercase tracking-[0.14em] text-dim", prominent ? "text-[10.5px]" : "text-[9.5px]")}>Digest of the last</span>
      <div role="radiogroup" aria-label="Period" className="flex items-center rounded-md border border-edge bg-black/30 p-0.5">
        {DIGEST_RUN_DAYS.map((d) => {
          const on = run.days === d;
          return (
            <button
              key={d}
              role="radio"
              aria-checked={on}
              onClick={() => run.setDays(d)}
              className={cx(
                "rounded px-2 font-mono tracking-[0.06em] transition",
                prominent ? "h-7 text-[11px]" : "h-6 text-[10px]",
                on ? "bg-violet-400/15 text-violet-100 shadow-[inset_0_0_0_1px_rgba(196,181,253,0.35)]" : "text-dim hover:text-slate-200",
              )}
            >
              {d} day{d === 1 ? "" : "s"}
            </button>
          );
        })}
      </div>
      <button
        onClick={run.start}
        className={cx(
          "rounded-md border border-violet-400/50 bg-violet-500/15 font-mono font-semibold uppercase tracking-[0.16em] text-violet-100 transition hover:bg-violet-500/25",
          prominent ? "h-8 px-4 text-[11px]" : "h-6 px-2.5 text-[9.5px]",
        )}
      >
        Run
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ view */

export function DigestView({
  digests,
  followups,
  onAsk,
  switcher,
  run,
  className,
}: {
  /** newest first */
  digests: Digest[];
  followups: FollowUp[];
  onAsk: (followupId: string) => void;
  switcher: ReactNode;
  /** on-demand digest for a period (POST /digests/run); omitted when unsupported */
  run?: DigestRun & { finishedWithDigest: number };
  className?: string;
}) {
  const [picked, setPicked] = useState<string | null>(null); // null = follow the latest
  // A run that produced a digest jumps to it.
  const finished = run?.finishedWithDigest ?? 0;
  useEffect(() => {
    if (finished) setPicked(null);
  }, [finished]);
  const latest = digests[0] ?? null;
  const digest = digests.find((d) => d.id === picked) ?? latest;
  const fuById = useMemo(() => new Map(followups.map((f) => [f.id, f])), [followups]);
  const groups = useMemo(() => (digest ? groupThreads(digest.threads) : []), [digest]);
  const asks = digest?.threads.filter((t) => t.asks_me).length ?? 0;

  // A picked digest that disappears (node switch) falls back to the latest.
  useEffect(() => {
    if (picked && !digests.some((d) => d.id === picked)) setPicked(null);
  }, [digests, picked]);

  return (
    <section className={cx("panel flex min-w-0 flex-col", className)}>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge/80 px-4 py-2.5">
        <div className="flex items-center gap-3">
          <h2 className="label flex items-center gap-2 !text-slate-300">
            <span className="text-signal/60">10</span>
            <span className="text-signal/30">//</span>
            Follow-ups
          </h2>
          {switcher}
        </div>
        {run && digests.length > 0 && <DigestRunControl run={run} />}
        {digests.length > 0 && (
          <label className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-dim">
            Digest
            <select
              value={digest?.id ?? ""}
              onChange={(e) => setPicked(e.target.value === latest?.id ? null : e.target.value)}
              className="h-7 max-w-[340px] rounded-md border border-edge bg-black/40 px-2 font-mono text-[10.5px] normal-case tracking-normal text-slate-200 [color-scheme:dark] focus:border-signal/60 focus:outline-none"
            >
              {digests.map((d, i) => (
                <option key={d.id} value={d.id}>
                  {i === 0 ? "Latest · " : ""}
                  {when(d.created_at)} · {d.threads.length} thread{d.threads.length === 1 ? "" : "s"}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>

      {!digest ? (
        <div className="flex min-h-[360px] flex-col items-center justify-center gap-3 px-6 py-10 text-center">
          <svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" className="text-mute" aria-hidden>
            <rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.25" />
            <path d="M2.25 4l5.75 4.5L13.75 4" />
          </svg>
          {run?.outcome && !run.running ? (
            <p className="max-w-lg rounded-md border border-violet-400/30 bg-violet-500/[0.07] px-3 py-2 text-[13px] text-violet-100" role="status">
              {run.outcome}
            </p>
          ) : (
            <p className="text-[13px] text-slate-300">No CC digest yet.</p>
          )}
          <p className="max-w-lg text-[12.5px] leading-relaxed text-slate-400">
            The digest summarizes CC emails that arrive after each scan. For email you&apos;ve already scanned, run a digest for a period:
          </p>
          {run && (
            <div className="mt-1 rounded-lg border border-edge/80 bg-black/25 px-4 py-3">
              <DigestRunControl run={run} prominent />
            </div>
          )}
          {run?.error && <p className="font-mono text-[11px] text-red-400">{run.error}</p>}
        </div>
      ) : (
        <div className="flex flex-col gap-4 p-4 pb-0">
          {run && (run.error || (run.outcome && !run.running)) && (
            <p
              className={cx(
                "rounded-md border px-3 py-2 text-[12px]",
                run.error ? "border-red-500/40 bg-red-500/[0.07] text-red-200" : "border-violet-400/30 bg-violet-500/[0.07] text-violet-100",
              )}
              role="status"
            >
              {run.error ?? run.outcome}
            </p>
          )}
          {/* briefing */}
          <div className="rounded-lg border border-violet-400/25 bg-gradient-to-br from-violet-500/[0.07] via-transparent to-transparent px-4 py-3.5">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] uppercase tracking-[0.16em] text-dim">
              <span className="text-violet-300">CC digest</span>
              <span>
                since <span className="text-slate-300 normal-case tracking-normal">{when(digest.window_start)}</span>
              </span>
              <span className="text-mute">·</span>
              <span>
                {digest.threads.length} thread{digest.threads.length === 1 ? "" : "s"}
              </span>
              {asks > 0 && <span className="text-amber-300">{asks} ask{asks === 1 ? "s" : ""} you</span>}
              {digest.skipped > 0 && <span className="text-mute normal-case tracking-normal">{digest.skipped} skipped (automated or by rules)</span>}
              {digest.id !== latest?.id && (
                <button onClick={() => setPicked(null)} className="ml-auto text-signal hover:underline">
                  Back to latest →
                </button>
              )}
            </div>
            {digest.headline.length > 0 && (
              <ul className="mt-2.5 grid gap-1.5">
                {digest.headline.slice(0, 3).map((h, i) => (
                  <li key={i} className="flex gap-2.5 text-[15.5px] leading-snug text-ink">
                    <span className="mt-[9px] h-1.5 w-1.5 shrink-0 rounded-full bg-violet-300" />
                    <span className="min-w-0">{h}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* threads by project: groups flow into two masonry columns so single-thread groups don't leave gaps */}
          <div className="gap-4 lg:columns-2 [&>section]:mb-4">
          {groups.map((g) => {
            const color = g.project ? projectColor(g.project) : "#64748b";
            return (
              <section key={g.project ?? "__untagged"} className="break-inside-avoid">
                <h3 className="mb-2 flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.18em]">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
                  <span style={{ color: g.project ? color : "#7b879f" }}>{g.project ?? "Untagged"}</span>
                  <span className="text-mute">· {g.threads.length}</span>
                  <span className="h-px flex-1 bg-edge/70" />
                </h3>
                <div className="grid grid-cols-1 gap-3">
                  {g.threads.map((t) => (
                    <ThreadCard key={t.conversation_id} t={t} followup={t.followup_id ? fuById.get(t.followup_id) : undefined} onAsk={onAsk} />
                  ))}
                </div>
              </section>
            );
          })}
          </div>
        </div>
      )}
    </section>
  );
}
