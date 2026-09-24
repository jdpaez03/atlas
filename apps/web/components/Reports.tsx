"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { AgentDefinition, AgentReport, Attachment, Claim, ClaimKind, Confidence, Evidence, MissionReport, Task } from "@/lib/contracts";
import { CLAIM, cx, hms } from "@/lib/ui";
import { FileLink } from "./Files";
import { Empty, Panel } from "./primitives";

/* ---------------------------------------------------------------- evidence (system-recorded actions) */

const EVIDENCE: Record<Evidence["kind"], { label: string; color: string }> = {
  file_read: { label: "Read", color: "#38bdf8" },
  file_listed: { label: "Listed", color: "#94a3b8" },
  file_written: { label: "Wrote", color: "#34d399" },
  web_search: { label: "Search", color: "#a78bfa" },
  web_fetch: { label: "Fetch", color: "#c4b5fd" },
  consult: { label: "Consult", color: "#2dd4bf" },
  approval: { label: "Approval", color: "#f59e0b" },
};

export function EvidenceIcon({ kind, color, size = 12 }: { kind: Evidence["kind"]; color: string; size?: number }) {
  const p = { width: size, height: size, viewBox: "0 0 16 16", fill: "none", stroke: color, strokeWidth: 1.4, strokeLinecap: "round", strokeLinejoin: "round" } as const;
  switch (kind) {
    case "file_read":
      return (
        <svg {...p} aria-hidden>
          <path d="M4 1.75h5.5l3 3v9.5H4z" />
          <path d="M6 8h4.5M6 10.5h3" />
        </svg>
      );
    case "file_listed":
      return (
        <svg {...p} aria-hidden>
          <path d="M1.75 4.25h4.5l1.5 1.5h6.5v7.5H1.75z" />
        </svg>
      );
    case "file_written":
      return (
        <svg {...p} aria-hidden>
          <path d="M4 1.75h5.5l3 3v2.5M4 1.75v12.5h3.5" />
          <path d="M9.5 13.75l.4-1.9 3.6-3.6 1.5 1.5-3.6 3.6z" />
        </svg>
      );
    case "web_search":
      return (
        <svg {...p} aria-hidden>
          <circle cx="7" cy="7" r="4.25" />
          <path d="M10.2 10.2l3.8 3.8" />
        </svg>
      );
    case "web_fetch":
      return (
        <svg {...p} aria-hidden>
          <circle cx="8" cy="8" r="6.25" />
          <path d="M1.75 8h12.5M8 1.75c2 2 2 10.5 0 12.5M8 1.75c-2 2-2 10.5 0 12.5" />
        </svg>
      );
    case "consult":
      return (
        <svg {...p} aria-hidden>
          <path d="M2 3.25h8v5.5H5.5L3 10.75V8.75H2z" />
          <path d="M12 6.25h2v5.5h-1v2l-2.5-2H7.5v-1.5" />
        </svg>
      );
    case "approval":
      return (
        <svg {...p} aria-hidden>
          <path d="M8 1.75l5.25 2v4c0 3.2-2.3 5.4-5.25 6.5C5.05 13.15 2.75 10.95 2.75 7.75v-4z" />
          <path d="M5.75 8l1.6 1.6 3-3.2" />
        </svg>
      );
  }
}

/** Short, readable form of an evidence ref: a file's name, a URL's host + path, or an agent's name. */
function shortRef(e: Evidence, agents: Map<string, AgentDefinition>): string {
  if (e.kind === "consult") return agents.get(e.ref)?.name ?? e.ref.toUpperCase();
  if (e.kind === "approval") return e.ref.length > 14 ? `${e.ref.slice(0, 12)}…` : e.ref;
  if (e.kind === "web_search") return `“${e.ref}”`;
  if (/^https?:\/\//.test(e.ref)) {
    try {
      const u = new URL(e.ref);
      const path = u.pathname.replace(/\/$/, "");
      const tail = path.split("/").filter(Boolean).pop();
      return `${u.host.replace(/^www\./, "")}${tail ? `/…/${tail}` : ""}`;
    } catch {
      return e.ref;
    }
  }
  const parts = e.ref.split(/[\\/]/).filter(Boolean);
  if (e.kind === "file_listed") return parts.length ? `${parts[parts.length - 1]}/` : e.ref;
  return parts.pop() ?? e.ref;
}

function EvidenceList({ items, agents }: { items: Evidence[]; agents: Map<string, AgentDefinition> }) {
  const failed = items.filter((e) => !e.ok).length;
  const color = "#2dd4bf";
  return (
    <section className="rounded-lg border bg-black/15 p-3" style={{ borderColor: items.length ? `${color}40` : "#1a2340" }}>
      <h4 className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: items.length ? color : "#4a5670" }}>
        <EvidenceIcon kind="file_read" color={items.length ? color : "#4a5670"} size={11} />
        Evidence
        <span className="text-mute">· {items.length}</span>
        {failed > 0 && <span className="text-red-400/90 normal-case tracking-normal">{failed} failed</span>}
        <span className="ml-auto normal-case tracking-normal text-mute">recorded by the system</span>
      </h4>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-mute">No recorded actions — this agent used no tools.</p>
      ) : (
        <ul className="grid gap-px overflow-hidden rounded-md border border-edge/60">
          {[...items]
            .sort((a, b) => a.at.localeCompare(b.at))
            .map((e) => {
              const k = EVIDENCE[e.kind] ?? EVIDENCE.file_read;
              return (
                <li
                  key={e.id}
                  className={cx("grid grid-cols-[14px_52px_minmax(0,1fr)_auto] items-center gap-2 px-2 py-1.5", e.ok ? "bg-black/20" : "bg-red-500/[0.07]")}
                  title={`${e.ref}${e.detail ? `\n${e.detail}` : ""}`}
                >
                  <EvidenceIcon kind={e.kind} color={e.ok ? k.color : "#f87171"} />
                  <span className="font-mono text-[9px] uppercase tracking-[0.14em]" style={{ color: e.ok ? k.color : "#f87171" }}>
                    {k.label}
                  </span>
                  <span className="min-w-0">
                    <span className={cx("block truncate font-mono text-[11px]", e.ok ? "text-slate-200" : "text-red-200/90 line-through decoration-red-400/50")}>{shortRef(e, agents)}</span>
                    {e.detail && <span className={cx("block truncate text-[10.5px]", e.ok ? "text-mute" : "text-red-300/80")}>{e.ok ? e.detail : `Failed — ${e.detail}`}</span>}
                  </span>
                  <span className="flex items-center gap-1.5 font-mono text-[9.5px] tabular-nums text-mute">
                    <span className={e.ok ? "text-emerald-400" : "text-red-400"} aria-label={e.ok ? "ok" : "failed"}>
                      {e.ok ? "✓" : "✕"}
                    </span>
                    {hms(e.at)}
                  </span>
                </li>
              );
            })}
        </ul>
      )}
    </section>
  );
}

function Deliverables({ items, title = "Deliverables", empty }: { items: Attachment[]; title?: string; empty?: string }) {
  const color = "#34d399";
  if (!items.length && !empty) return null;
  return (
    <section className="rounded-lg border bg-black/15 p-3" style={{ borderColor: items.length ? `${color}40` : "#1a2340" }}>
      <h4 className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: items.length ? color : "#4a5670" }}>
        <EvidenceIcon kind="file_written" color={items.length ? color : "#4a5670"} size={11} />
        {title}
        <span className="text-mute">· {items.length}</span>
      </h4>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-mute">{empty}</p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((a) => (
            <li key={a.id}>
              <FileLink a={a} accent={color} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const isUnverified = (s: string) => /^unverified:/i.test(s.trim());

function Limitations({ items }: { items: string[] }) {
  const color = "#f97316";
  const unverified = items.filter(isUnverified).length;
  return (
    <section className="rounded-lg border bg-black/15 p-3" style={{ borderColor: unverified ? "#ef444466" : items.length ? `${color}40` : "#1a2340" }}>
      <h4 className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: items.length ? color : "#4a5670" }}>
        <span className="text-[11px]">⚠</span>
        Limitations
        <span className="text-mute">· {items.length}</span>
        {unverified > 0 && <span className="normal-case tracking-normal text-red-400">{unverified} unverified claim{unverified === 1 ? "" : "s"}</span>}
      </h4>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-mute">None.</p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((t, i) =>
            isUnverified(t) ? (
              <li
                key={i}
                className="flex gap-2 rounded border border-red-400/40 bg-red-500/[0.08] px-2 py-1 text-[12px] leading-snug text-red-100"
                title="The report claims something the system has no record of — treat it as unverified."
              >
                <span className="mt-[1px] shrink-0 font-mono text-[9px] font-semibold uppercase tracking-[0.16em] text-red-300">No record</span>
                <span>{t.replace(/^unverified:\s*/i, "")}</span>
              </li>
            ) : (
              <li key={i} className="flex gap-2 text-[12px] leading-snug text-slate-300">
                <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full" style={{ background: color }} />
                {t}
              </li>
            ),
          )}
        </ul>
      )}
    </section>
  );
}

const KINDS: ClaimKind[] = ["FACT", "SCENARIO", "ASSUMPTION", "RECOMMENDATION"];

function ConfidenceMeter({ c }: { c: Confidence }) {
  const n = c === "HIGH" ? 3 : c === "MEDIUM" ? 2 : 1;
  const color = c === "HIGH" ? "#22c55e" : c === "MEDIUM" ? "#eab308" : "#f97316";
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[8.5px] uppercase tracking-[0.14em] text-dim" title={`Confidence: ${c}`}>
      <span className="flex items-end gap-[2px]">
        {[1, 2, 3].map((i) => (
          <span key={i} className="w-[3px] rounded-sm" style={{ height: 3 + i * 2.5, background: i <= n ? color : "#26324f" }} />
        ))}
      </span>
      {c}
    </span>
  );
}

function ClaimIcon({ kind, color }: { kind: ClaimKind; color: string }) {
  const p = { width: 11, height: 11, viewBox: "0 0 16 16", fill: "none", stroke: color, strokeWidth: 1.6 } as const;
  switch (kind) {
    case "FACT":
      return (
        <svg {...p} aria-hidden>
          <rect x="3" y="3" width="10" height="10" rx="1.5" fill={`${color}33`} />
        </svg>
      );
    case "ASSUMPTION":
      return (
        <svg {...p} aria-hidden>
          <circle cx="8" cy="8" r="5.5" strokeDasharray="2 2" />
        </svg>
      );
    case "SCENARIO":
      return (
        <svg {...p} aria-hidden>
          <path d="M3 13V8m0 0l5-5m-5 5l10 0M8 3l5 5" strokeLinecap="round" />
        </svg>
      );
    case "RECOMMENDATION":
      return (
        <svg {...p} aria-hidden>
          <path d="M3 8h9M8.5 4l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
  }
}

function ClaimItem({ c }: { c: Claim }) {
  const k = CLAIM[c.kind];
  const style =
    c.kind === "ASSUMPTION"
      ? { borderStyle: "dashed", borderColor: `${k.color}66`, background: `${k.color}08` }
      : c.kind === "RECOMMENDATION"
        ? { borderColor: `${k.color}66`, background: `linear-gradient(90deg, ${k.color}1a, transparent)` }
        : { borderColor: `${k.color}33`, borderLeftColor: k.color, background: `${k.color}0a` };
  return (
    <li className={cx("rounded-md border px-3 py-2", c.kind !== "ASSUMPTION" && c.kind !== "RECOMMENDATION" && "border-l-2")} style={style}>
      <p className={cx("text-[12.5px] leading-snug", c.kind === "ASSUMPTION" ? "text-amber-100/80 italic" : c.kind === "RECOMMENDATION" ? "font-medium text-emerald-50" : "text-slate-200")}>
        {c.statement}
      </p>
      <div className="mt-1 flex flex-wrap items-center gap-3">
        <ConfidenceMeter c={c.confidence} />
        {c.sources.length > 0 && <span className="truncate font-mono text-[9.5px] text-mute">src: {c.sources.join(" · ")}</span>}
      </div>
    </li>
  );
}

function Findings({ claims }: { claims: Claim[] }) {
  const groups = KINDS.map((k) => ({ k, items: claims.filter((c) => c.kind === k) })).filter((g) => g.items.length);
  if (!groups.length) return <p className="text-[12px] text-mute">No findings.</p>;
  return (
    <div className="grid gap-4">
      {groups.map(({ k, items }) => (
        <section key={k}>
          <h4 className="mb-1.5 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: CLAIM[k].color }}>
            <ClaimIcon kind={k} color={CLAIM[k].color} />
            {CLAIM[k].label}s <span className="text-mute">· {items.length}</span>
            <span className="ml-1 normal-case tracking-normal text-mute">{CLAIM[k].hint}</span>
          </h4>
          <ul className="grid gap-1.5">
            {items.map((c, i) => (
              <ClaimItem key={i} c={c} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function Block({
  title,
  color,
  items,
  icon,
  empty,
  flagged,
}: {
  title: string;
  color: string;
  items: string[];
  icon: ReactNode;
  empty?: string;
  /** items the system has no record of (from "Unverified: …" limitations) */
  flagged?: (item: string) => boolean;
}) {
  return (
    <section className="rounded-lg border bg-black/15 p-3" style={{ borderColor: items.length ? `${color}40` : "#1a2340" }}>
      <h4 className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ color: items.length ? color : "#4a5670" }}>
        {icon}
        {title}
        <span className="text-mute">· {items.length}</span>
      </h4>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-mute">{empty ?? "None."}</p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((t, i) =>
            flagged?.(t) ? (
              <li key={i} className="flex gap-2 text-[12px] leading-snug text-red-200" title="No system record of this — unverified">
                <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-red-400" />
                <span className="underline decoration-red-400/60 decoration-wavy underline-offset-[3px]">{t}</span>
              </li>
            ) : (
              <li key={i} className="flex gap-2 text-[12px] leading-snug text-slate-300">
                <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full" style={{ background: color }} />
                {t}
              </li>
            ),
          )}
        </ul>
      )}
    </section>
  );
}

const OBJ: Record<MissionReport["objective_status"], { color: string; label: string }> = {
  ACHIEVED: { color: "#22c55e", label: "Objective achieved" },
  PARTIAL: { color: "#eab308", label: "Partially achieved" },
  NOT_ACHIEVED: { color: "#ef4444", label: "Not achieved" },
};

function MissionReportView({
  r,
  agents,
  reports,
  versions,
  onVersion,
}: {
  r: MissionReport;
  agents: Map<string, AgentDefinition>;
  reports: AgentReport[];
  versions: MissionReport[];
  onVersion: (id: string) => void;
}) {
  const o = OBJ[r.objective_status] ?? OBJ.PARTIAL;
  const latest = versions[versions.length - 1];
  const contributors = [...new Set(reports.map((x) => x.agent_id))];
  return (
    <div className="grid gap-5 p-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.2em]" style={{ color: o.color, borderColor: `${o.color}55`, background: `${o.color}14` }}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: o.color, boxShadow: `0 0 8px ${o.color}` }} />
            {o.label}
          </span>
          <span className="font-mono text-[10px] text-mute">
            {r.tasks_completed.length} tasks completed · {r.tasks_pending.length} pending · issued {hms(r.created_at)}
          </span>
          {versions.length > 1 && (
            <div className="ml-auto flex items-center gap-1" role="radiogroup" aria-label="Report version">
              <span className="mr-1 font-mono text-[9px] uppercase tracking-[0.2em] text-mute">Version</span>
              {versions.map((v) => {
                const on = v.id === r.id;
                return (
                  <button
                    key={v.id}
                    role="radio"
                    aria-checked={on}
                    onClick={() => onVersion(v.id)}
                    title={`Report v${v.version ?? 1} · ${hms(v.created_at)}`}
                    className={cx(
                      "rounded border px-1.5 py-[1px] font-mono text-[10px] tabular-nums transition",
                      on ? "border-signal/60 bg-signal/15 text-signal" : "border-edge-2 text-dim hover:text-slate-200",
                    )}
                  >
                    v{v.version ?? 1}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        {latest && r.id !== latest.id && (
          <p className="mt-2 font-mono text-[10px] text-amber-300/80">
            Viewing an earlier version.{" "}
            <button className="underline decoration-amber-300/40 underline-offset-2 hover:text-amber-200" onClick={() => onVersion(latest.id)}>
              Show latest (v{latest.version ?? versions.length})
            </button>
          </p>
        )}
        <h3 className="label mt-4 !text-signal/70">Executive summary</h3>
        <p className="mt-1.5 border-l-2 border-signal/40 pl-4 text-[15px] leading-relaxed font-light text-slate-100">{r.executive_summary}</p>
        {contributors.length > 0 && (
          <p className="mt-2 pl-4 font-mono text-[10px] text-mute">
            Consolidated from{" "}
            {contributors.map((id, i) => (
              <span key={id}>
                {i > 0 && ", "}
                <span style={{ color: agents.get(id)?.color }}>{agents.get(id)?.name ?? id}</span>
              </span>
            ))}
          </p>
        )}
        <h3 className="label mt-5 mb-2 !text-signal/70">Key findings</h3>
        <Findings claims={r.key_findings} />
      </div>
      <div className="grid content-start gap-3">
        <Deliverables items={r.deliverables ?? []} title="Mission deliverables" />
        <Block
          title="Needs human attention"
          color="#f59e0b"
          items={r.needs_human_attention}
          icon={<span className="text-[11px]">!</span>}
          empty="Nothing requires your decision."
        />
        <Block title="Conflicts" color="#ef4444" items={r.conflicts} icon={<span className="text-[11px]">⇄</span>} empty="No conflicts between agents." />
        <Block title="Assumptions" color="#eab308" items={r.assumptions} icon={<span className="text-[11px]">~</span>} />
        <Block title="Next actions" color="#22c55e" items={r.next_actions} icon={<span className="text-[11px]">→</span>} />
      </div>
    </div>
  );
}

/** "Unverified: <item> (no system record)" → "<item>" */
const unverifiedItems = (limitations: string[]) =>
  limitations
    .filter(isUnverified)
    .map((l) => l.replace(/^unverified:\s*/i, "").replace(/\s*\(no system record\)\s*$/i, "").trim().toLowerCase())
    .filter(Boolean);

function AgentReportView({ r, agent, task, agents }: { r: AgentReport; agent?: AgentDefinition; task?: Task; agents: Map<string, AgentDefinition> }) {
  const unverified = unverifiedItems(r.limitations);
  const flagged = unverified.length ? (t: string) => unverified.some((u) => t.toLowerCase().includes(u)) : undefined;
  return (
    <div className="grid gap-5 p-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-[15px] font-semibold tracking-[0.2em]" style={{ color: agent?.color }}>
            {agent?.name ?? r.agent_id}
          </span>
          <span className="label !text-[9px]">{agent?.title}</span>
          <ConfidenceMeter c={r.confidence} />
          <span className="font-mono text-[10px] text-mute">{hms(r.created_at)}</span>
        </div>
        {task && (
          <p className="mt-1 font-mono text-[10.5px] text-dim">
            Task · {task.title}
            {(task.round ?? 1) > 1 && <span className="ml-2 text-violet-300/80">round {task.round}</span>}
          </p>
        )}
        <h3 className="label mt-4 !text-signal/70">Asked to</h3>
        <p className="mt-1 border-l-2 pl-4 text-[14px] leading-relaxed font-light text-slate-100" style={{ borderColor: `${agent?.color ?? "#7dd3fc"}88` }}>
          {r.asked_to}
        </p>
        <h3 className="label mt-5 mb-2 !text-signal/70">Findings</h3>
        <Findings claims={r.findings} />
        <div className="mt-5">
          <EvidenceList items={r.evidence ?? []} agents={agents} />
        </div>
      </div>
      <div className="grid content-start gap-3">
        <Deliverables items={r.deliverables ?? []} />
        {r.limitations.length > 0 && <Limitations items={r.limitations} />}
        <Block title="Actions taken" color="#7dd3fc" items={r.actions_taken} icon={<span className="text-[11px]">✓</span>} flagged={flagged} />
        <Block title="Inputs used" color="#94a3b8" items={r.inputs_used} icon={<span className="text-[11px]">◇</span>} flagged={flagged} />
        <Block title="Unresolved" color="#f59e0b" items={r.unresolved} icon={<span className="text-[11px]">?</span>} />
        {r.limitations.length === 0 && <Limitations items={r.limitations} />}
        {r.needs_agents.length > 0 && <Block title="Needs agents" color="#a78bfa" items={r.needs_agents.map((x) => x.toUpperCase())} icon={<span className="text-[11px]">+</span>} />}
      </div>
    </div>
  );
}

export function Reports({
  missionReports,
  agentReports,
  agents,
  tasks,
  missionActive,
  missionClosed,
  className,
}: {
  /** every MissionReport of the selected mission (one per round) */
  missionReports: MissionReport[];
  agentReports: AgentReport[];
  agents: Map<string, AgentDefinition>;
  tasks: Map<string, Task>;
  missionActive: boolean;
  missionClosed?: boolean;
  className?: string;
}) {
  const [tab, setTab] = useState<string>("atlas");
  useEffect(() => {
    if (tab !== "atlas" && !agentReports.some((r) => r.id === tab)) setTab("atlas");
  }, [agentReports, tab]);
  const current = agentReports.find((r) => r.id === tab);

  // Versions oldest → newest; the latest is shown unless the user picks another.
  const versions = [...missionReports].sort((a, b) => (a.version ?? 1) - (b.version ?? 1) || a.created_at.localeCompare(b.created_at));
  const [pickedVersion, setPickedVersion] = useState<string | null>(null);
  const latestId = versions[versions.length - 1]?.id ?? null;
  useEffect(() => setPickedVersion(null), [latestId]);
  const missionReport = versions.find((v) => v.id === pickedVersion) ?? versions[versions.length - 1];

  return (
    <Panel
      code="07"
      title="Reports"
      className={className}
      meta={
        <span>
          {agentReports.length} agent reports
          {missionReport ? ` · mission report ready${versions.length > 1 ? ` · v${versions[versions.length - 1].version}` : ""}` : ""}
        </span>
      }
    >
      <div className="flex gap-1 overflow-x-auto border-b border-edge/60 px-3 pt-2 scroll-thin" role="tablist">
        <TabButton active={tab === "atlas"} onClick={() => setTab("atlas")} color="#e2e8f0" ready={!!missionReport}>
          ATLAS · Mission report
        </TabButton>
        {agentReports.map((r) => {
          const a = agents.get(r.agent_id);
          const flagged = r.limitations.some(isUnverified) || (r.evidence ?? []).some((e) => !e.ok);
          return (
            <TabButton key={r.id} active={tab === r.id} onClick={() => setTab(r.id)} color={a?.color ?? "#94a3b8"} ready>
              {a?.name ?? r.agent_id}
              {flagged && (
                <span className="text-red-400" title="Unverified claims or failed actions">
                  !
                </span>
              )}
              {agentReports.filter((x) => x.agent_id === r.agent_id).length > 1 && tasks.get(r.task_id) ? (
                <span className="ml-1 normal-case tracking-normal opacity-60">
                  · {truncate(tasks.get(r.task_id)!.title, 18)}
                </span>
              ) : null}
            </TabButton>
          );
        })}
      </div>
      {tab === "atlas" ? (
        missionReport ? (
          <MissionReportView r={missionReport} agents={agents} reports={agentReports} versions={versions} onVersion={setPickedVersion} />
        ) : (
          <Empty className="min-h-40">
            {missionClosed
              ? `No mission report was issued for this mission. ${agentReports.length} agent report${agentReports.length === 1 ? "" : "s"} available.`
              : missionActive
              ? `Mission report pending — ATLAS consolidates once agents deliver. ${agentReports.length} agent report${agentReports.length === 1 ? "" : "s"} in so far.`
              : "Reports appear here once a mission runs."}
          </Empty>
        )
      ) : current ? (
        <AgentReportView r={current} agent={agents.get(current.agent_id)} task={tasks.get(current.task_id)} agents={agents} />
      ) : null}
    </Panel>
  );
}

function TabButton({ active, onClick, color, ready, children }: { active: boolean; onClick: () => void; color: string; ready: boolean; children: ReactNode }) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cx(
        "relative -mb-px flex shrink-0 items-center gap-2 rounded-t-md border border-b-0 px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] transition",
        active ? "border-edge bg-panel-2 text-ink" : "border-transparent text-dim hover:text-slate-200",
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: ready ? color : "#26324f" }} />
      {children}
      {active && <span className="absolute inset-x-2 top-0 h-px" style={{ background: color }} />}
    </button>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
