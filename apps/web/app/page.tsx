"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityFeed } from "@/components/ActivityFeed";
import { AgentBoard, type DivisionGroup } from "@/components/AgentBoard";
import { ApprovalQueue } from "@/components/ApprovalQueue";
import { CollabGraph } from "@/components/CollabGraph";
import { DigestView, FollowUpsTabs, useDigestRun, type FollowUpsTab } from "@/components/Digest";
import { DraftDrawer } from "@/components/DraftDrawer";
import { FollowUpsBoard, type FollowUpActions } from "@/components/FollowUps";
import { Header, type View } from "@/components/Header";
import { InboxChip } from "@/components/InboxChip";
import { MissionHistory } from "@/components/MissionHistory";
import { MissionPanel } from "@/components/MissionPanel";
import { MissionThread } from "@/components/MissionThread";
import { MonitorView } from "@/components/Monitor";
import { Emblem } from "@/components/primitives";
import { Reports } from "@/components/Reports";
import { TaskBoard } from "@/components/TaskBoard";
import { API_URL, type AtlasConfig, type Availability, type MissionSummary } from "@/lib/api";
import type { AgentDefinition, Task } from "@/lib/contracts";
import { followupCounts, proposedDrafts } from "@/lib/followups";
import { useAtlas } from "@/lib/store";
import { STATUS, msgFrom, msgTo, useNow } from "@/lib/ui";

const DIGEST_SEEN_KEY = "atlas.digest.lastSeen";
const INBOX_EVENTS = new Set(["followup.upserted", "draft.upserted", "digest.ready"]);
const MONITOR_EVENTS = new Set(["alert.upserted", "rock.updated", "brief.ready"]);

function initialView(): View {
  if (typeof window === "undefined") return "missions";
  const v = new URLSearchParams(window.location.search).get("view");
  return v === "followups" || v === "monitor" ? v : "missions";
}

export default function CommandCenter() {
  const atlas = useAtlas();
  const { world, feed, conn, loaded, ready, config: loadConfig, availability: loadAvailability } = atlas;

  /* ---- Phase 2: live-mode config + agent availability (404 → no live / all available) */
  const [config, setConfig] = useState<AtlasConfig | null>(null);
  const [availability, setAvailability] = useState<Availability>({});
  useEffect(() => {
    if (!ready) return;
    let alive = true;
    loadConfig().then((c) => alive && setConfig(c));
    loadAvailability().then((a) => alive && setAvailability(a));
    return () => {
      alive = false;
    };
  }, [ready, conn, loadConfig, loadAvailability]);

  /* ---- node selection */
  const [nodeId, setNodeId] = useState<string | null>(null);
  useEffect(() => {
    if (!nodeId && world.nodes.length) setNodeId((world.nodes.find((n) => n.enabled) ?? world.nodes[0]).id);
  }, [world.nodes, nodeId]);
  const node = world.nodes.find((n) => n.id === nodeId) ?? null;

  /* ---- organization for this node */
  const org = useMemo(() => {
    const inNode = (a: AgentDefinition) => a.enabled !== false && (a.nodes?.includes("*") || (nodeId !== null && a.nodes?.includes(nodeId)));
    const visible = world.agents.filter(inNode);
    const divs = world.divisions.filter((d) => d.node === nodeId);
    const divisions: DivisionGroup[] = divs
      .map((division) => ({ division, members: visible.filter((a) => a.division === division.id) }))
      .filter((g) => g.members.length > 0);
    const divIds = new Set(divisions.map((g) => g.division.id));
    return {
      visible,
      orchestrator: visible.find((a) => a.is_orchestrator),
      core: visible.filter((a) => !a.is_orchestrator && !(a.division && divIds.has(a.division))),
      divisions,
    };
  }, [world.agents, world.divisions, nodeId]);

  const agents = useMemo(() => new Map(world.agents.map((a) => [a.id, a])), [world.agents]);
  const states = useMemo(() => new Map(world.agent_states.map((s) => [s.agent_id, s])), [world.agent_states]);
  const tasksById = useMemo(() => new Map(world.tasks.map((t) => [t.id, t])), [world.tasks]);

  /* ---- mission selection (follows the latest unless the user picks one) */
  const nodeMissions = useMemo(
    () => world.missions.filter((m) => m.node === nodeId).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [world.missions, nodeId],
  );
  const [picked, setPicked] = useState<string | null>(null);
  const mission = nodeMissions.find((m) => m.id === picked) ?? nodeMissions[0] ?? null;
  const mid = mission?.id ?? null;

  const tasks = useMemo(() => {
    if (!mission) return [] as Task[];
    const order = new Map(mission.task_ids.map((id, i) => [id, i]));
    return world.tasks
      .filter((t) => t.mission_id === mission.id)
      .sort((a, b) => (order.get(a.id) ?? 1e9) - (order.get(b.id) ?? 1e9) || a.created_at.localeCompare(b.created_at));
  }, [world.tasks, mission]);
  const messages = useMemo(() => world.messages.filter((m) => m.mission_id === mid), [world.messages, mid]);
  // The human ↔ ATLAS thread lives in its own panel; the graph shows agent-to-agent traffic only.
  const agentMessages = useMemo(() => messages.filter((m) => msgFrom(m) !== "human" && msgTo(m) !== "human"), [messages]);
  const agentReports = useMemo(() => world.agent_reports.filter((r) => r.mission_id === mid), [world.agent_reports, mid]);
  const missionReports = useMemo(() => world.mission_reports.filter((r) => r.mission_id === mid), [world.mission_reports, mid]);

  /* ---- Phase 3: mission history (GET /missions?node=; null → endpoint missing, hide the drawer) */
  const { missionHistory } = atlas;
  const [history, setHistory] = useState<MissionSummary[] | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const refreshHistory = useCallback(() => {
    if (!ready) return;
    setHistoryLoading(true);
    missionHistory(nodeId)
      .then(setHistory)
      .finally(() => setHistoryLoading(false));
  }, [ready, nodeId, missionHistory]);
  // Probe on connect / node change, and whenever a mission appears or changes phase.
  const missionsKey = nodeMissions.map((m) => `${m.id}:${m.phase}:${m.round}`).join("|");
  useEffect(() => {
    refreshHistory();
  }, [refreshHistory, missionsKey, conn]);
  const knownMissions = useMemo(() => new Set(world.missions.map((m) => m.id)), [world.missions]);

  const nodeMissionIds = useMemo(() => new Set(nodeMissions.map((m) => m.id)), [nodeMissions]);
  const approvals = useMemo(
    () => world.approvals.filter((a) => (a.state === "PENDING" ? nodeMissionIds.has(a.mission_id) : a.mission_id === mid)),
    [world.approvals, nodeMissionIds, mid],
  );
  const pendingCount = approvals.filter((a) => a.state === "PENDING").length;
  const events = useMemo(
    // mission.updated fires after every LLM call (usage) — it's noise in the feed.
    () => feed.filter((e) => e.type !== "mission.updated" && (!e.mission_id || nodeMissionIds.has(e.mission_id))),
    [feed, nodeMissionIds],
  );

  /* ---- Inbox: follow-ups & drafts (docs/INBOX.md) */
  const [view, setView] = useState<View>("missions");
  useEffect(() => setView(initialView()), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      if (e.key === "1") setView("missions");
      else if (e.key === "2") setView("followups");
      else if (e.key === "3") setView("monitor");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const followups = useMemo(() => (world.followups ?? []).filter((f) => !nodeId || f.node === nodeId), [world.followups, nodeId]);
  const drafts = useMemo(() => (world.drafts ?? []).filter((d) => !nodeId || d.node === nodeId), [world.drafts, nodeId]);
  /* ---- CC digest (docs/INBOX.md §4): Board | Digest, with an unread dot for a newer digest */
  const digests = useMemo(
    () => (world.digests ?? []).filter((d) => !nodeId || d.node === nodeId).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [world.digests, nodeId],
  );
  const [fuTab, setFuTab] = useState<FollowUpsTab>("board");
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("fu") === "digest") setFuTab("digest");
  }, []);
  const [digestSeen, setDigestSeen] = useState<string>("");
  useEffect(() => {
    try {
      setDigestSeen(localStorage.getItem(DIGEST_SEEN_KEY) ?? "");
    } catch {
      /* storage blocked */
    }
  }, []);
  const latestDigestAt = digests[0]?.created_at ?? "";
  const digestUnread = !!latestDigestAt && latestDigestAt > digestSeen;
  useEffect(() => {
    if (view !== "followups" || fuTab !== "digest" || !latestDigestAt || latestDigestAt <= digestSeen) return;
    setDigestSeen(latestDigestAt);
    try {
      localStorage.setItem(DIGEST_SEEN_KEY, latestDigestAt);
    } catch {
      /* storage blocked */
    }
  }, [view, fuTab, latestDigestAt, digestSeen]);
  const [highlightFu, setHighlightFu] = useState<string | null>(null);
  useEffect(() => {
    if (!highlightFu) return;
    const t = setTimeout(() => setHighlightFu(null), 2600);
    return () => clearTimeout(t);
  }, [highlightFu]);
  const showFollowup = useCallback((id: string) => {
    setFuTab("board");
    setHighlightFu(id);
  }, []);
  const digestRun = useDigestRun({
    run: atlas.inbox.runDigest,
    digests,
    missions: world.missions,
    reports: world.mission_reports,
    feed,
  });
  const fuSwitch = <FollowUpsTabs tab={fuTab} onTab={setFuTab} unread={digestUnread} />;

  const toReview = useMemo(() => proposedDrafts(drafts).sort((a, b) => b.created_at.localeCompare(a.created_at)), [drafts]);
  const fuNow = useNow(60_000);
  const fuCounts = useMemo(() => followupCounts(followups, fuNow), [followups, fuNow]);
  const [draftId, setDraftId] = useState<string | null>(null);
  const openDraft = world.drafts?.find((d) => d.id === draftId) ?? null;
  const openDraftFollowup = openDraft?.followup_id ? world.followups.find((f) => f.id === openDraft.followup_id) ?? null : null;
  // "Draft follow-up" → open the drawer as soon as ALFRED's draft lands.
  const [awaitDraftFor, setAwaitDraftFor] = useState<string | null>(null);
  useEffect(() => {
    if (!awaitDraftFor) return;
    const f = followups.find((x) => x.id === awaitDraftFor);
    const d = f?.draft_id ? drafts.find((x) => x.id === f.draft_id) : undefined;
    if (d?.status === "PROPOSED") {
      setDraftId(d.id);
      setAwaitDraftFor(null);
    }
  }, [awaitDraftFor, followups, drafts]);
  const { inbox } = atlas;
  const fuActions = useMemo<FollowUpActions>(
    () => ({
      patch: (id, p) => inbox.patchFollowup(id, p),
      draft: (id) => {
        setAwaitDraftFor(id);
        return inbox.draftFollowup(id);
      },
      openDraft: setDraftId,
    }),
    [inbox],
  );
  const inboxEvents = useMemo(
    () => feed.filter((e) => INBOX_EVENTS.has(e.type) || e.agent_id === "hermes" || (e.type === "evidence.recorded" && /email_read|draft_created/.test(JSON.stringify(e.payload?.evidence ?? "")))),
    [feed],
  );

  /* ---- ARGOS monitor (docs/ARGOS.md § UI) */
  const alerts = useMemo(() => (world.alerts ?? []).filter((a) => !nodeId || a.node === nodeId), [world.alerts, nodeId]);
  const highAlerts = alerts.filter((a) => a.status === "OPEN" && a.severity === "HIGH").length;
  const briefs = useMemo(
    () => (world.briefs ?? []).filter((b) => !nodeId || b.node === nodeId).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [world.briefs, nodeId],
  );
  const monitorEvents = useMemo(() => feed.filter((e) => MONITOR_EVENTS.has(e.type) || e.agent_id === "argos"), [feed]);

  const activeAgents = org.visible.filter((a) => STATUS[states.get(a.id)?.status ?? "IDLE"].active).length;

  if (!loaded) return <Boot conn={conn} />;

  return (
    <div className="min-h-screen">
      <Header
        nodes={world.nodes}
        node={nodeId}
        onNode={(id) => { setNodeId(id); setPicked(null); }}
        conn={conn}
        view={view}
        onView={setView}
        counts={fuCounts}
        highAlerts={highAlerts}
        inbox={<InboxChip inbox={inbox} ready={ready} conn={conn} />}
      />
      <DraftDrawer draft={openDraft} followup={openDraftFollowup} onClose={() => setDraftId(null)} decide={inbox.decideDraft} />
      {view === "monitor" ? (
        <main className="mx-auto flex max-w-[1680px] flex-col gap-4 px-4 py-4 lg:px-6">
          <MonitorView
            alerts={alerts}
            rocks={world.rocks ?? []}
            briefs={briefs}
            missions={world.missions}
            argos={atlas.argos}
            ready={ready}
            feed={<ActivityFeed events={monitorEvents} agents={agents} className="h-[380px]" />}
          />
          <footer className="flex items-center justify-between py-2 font-mono text-[9.5px] uppercase tracking-[0.22em] text-mute">
            <span>ARGOS · Dashboards, L10 and Rocks · reads only, never writes to the Suite</span>
            <span>
              {atlas.mode === "mock" ? "Simulated stream" : API_URL} · seq {world.last_seq}
            </span>
          </footer>
        </main>
      ) : view === "followups" ? (
        <main className="mx-auto flex max-w-[1680px] flex-col gap-4 px-4 py-4 lg:px-6">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_300px] 2xl:grid-cols-[minmax(0,1fr)_340px]">
            {fuTab === "digest" ? (
              <DigestView digests={digests} followups={followups} onAsk={showFollowup} switcher={fuSwitch} run={digestRun} className="min-h-[560px]" />
            ) : (
              <FollowUpsBoard
                followups={followups}
                drafts={drafts}
                actions={fuActions}
                inboxAvailable
                switcher={fuSwitch}
                highlight={highlightFu}
                className="min-h-[560px]"
              />
            )}
            <div className="flex min-w-0 flex-col gap-4">
              <ApprovalQueue approvals={approvals} agents={agents} decide={atlas.decide} drafts={toReview} onOpenDraft={setDraftId} />
              <ActivityFeed events={inboxEvents} agents={agents} className="h-[420px] xl:h-auto xl:min-h-[360px] xl:flex-1" />
            </div>
          </div>
          <footer className="flex items-center justify-between py-2 font-mono text-[9.5px] uppercase tracking-[0.22em] text-mute">
            <span>ATLAS · Follow-ups from your work email · never sends email</span>
            <span>
              {atlas.mode === "mock" ? "Simulated stream" : API_URL} · seq {world.last_seq}
            </span>
          </footer>
        </main>
      ) : (
      <main className="mx-auto flex max-w-[1680px] flex-col gap-4 px-4 py-4 lg:px-6">
        <MissionPanel
          mission={mission}
          missions={nodeMissions}
          node={node}
          tasks={tasks}
          activeAgents={activeAgents}
          totalAgents={org.visible.length}
          pendingApprovals={pendingCount}
          onSelectMission={setPicked}
          loadScenarios={atlas.scenarios}
          launch={atlas.launch}
          onLaunched={(m) => setPicked(m.id)}
          config={config}
          cancelMission={atlas.cancel}
          historyCount={history ? history.length : null}
          onOpenHistory={() => {
            setHistoryOpen(true);
            refreshHistory();
          }}
        />
        {history && (
          <MissionHistory
            open={historyOpen}
            onClose={() => setHistoryOpen(false)}
            items={history}
            loading={historyLoading}
            error={null}
            currentId={mid}
            known={knownMissions}
            onSelect={setPicked}
            onRefresh={refreshHistory}
            nodeName={node?.name ?? "—"}
          />
        )}

        {/* Three independent columns: each stretches to the tallest; the feed fills what's left. */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-[minmax(340px,380px)_minmax(0,1fr)_minmax(310px,350px)]">
          <div className="flex min-w-0 flex-col">
            <AgentBoard
              className="flex-1"
              orchestrator={org.orchestrator}
              core={org.core}
              divisions={org.divisions}
              states={states}
              tasks={tasksById}
              agents={agents}
              availability={availability}
            />
          </div>
          <div className="flex min-w-0 flex-col gap-4">
            <CollabGraph
              orchestrator={org.orchestrator}
              core={org.core}
              divisions={org.divisions}
              states={states}
              messages={agentMessages}
              agents={agents}
            />
            <TaskBoard className="flex-1" tasks={tasks} agents={agents} />
          </div>
          <div className={`flex min-w-0 flex-col gap-4 lg:col-span-2 xl:order-none xl:col-span-1 ${pendingCount > 0 ? "order-first" : ""}`}>
            <ApprovalQueue approvals={approvals} agents={agents} decide={atlas.decide} drafts={toReview} onOpenDraft={setDraftId} />
            {/* thread + feed share what's left; absolutely positioned so they never drive the row height */}
            <div className="relative h-[460px] xl:h-auto xl:min-h-[600px] xl:flex-1">
              <div className="absolute inset-0 grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-1 xl:grid-rows-[minmax(0,1.1fr)_minmax(0,1fr)]">
                <MissionThread
                  mission={mission}
                  messages={messages}
                  tasks={tasks}
                  agents={agents}
                  send={atlas.sendMessage}
                  attach={atlas.attach}
                  className="min-h-0"
                />
                <ActivityFeed events={events} agents={agents} className="min-h-0" />
              </div>
            </div>
          </div>
        </div>

        <Reports
          missionReports={missionReports}
          agentReports={agentReports}
          agents={agents}
          tasks={tasksById}
          missionActive={!!mission}
          missionClosed={mission?.phase === "CLOSED" || !!mission?.interrupted}
        />

        <footer className="flex items-center justify-between py-2 font-mono text-[9.5px] uppercase tracking-[0.22em] text-mute">
          <span>ATLAS · Task → Delegate → Collaborate → Review → Report</span>
          <span>
            {atlas.mode === "mock" ? "Simulated stream" : API_URL} · seq {world.last_seq}
          </span>
        </footer>
      </main>
      )}
    </div>
  );
}

function Boot({ conn }: { conn: string }) {
  const offline = conn === "offline";
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="panel w-full max-w-md p-8 text-center">
        <div className="flex justify-center">
          <Emblem size={56} color={offline ? "#ef4444" : "#7dd3fc"} />
        </div>
        <p className="mt-5 font-mono text-[18px] font-semibold tracking-[0.42em] text-ink">ATLAS</p>
        <p className="label mt-2">{offline ? "Uplink offline — retrying" : "Establishing uplink"}</p>
        {offline && (
          <p className="mt-4 text-[12px] leading-relaxed text-slate-400">
            Can't reach the ATLAS API at <code className="text-signal">{API_URL}</code>. Start it with{" "}
            <code className="text-slate-200">uv run uvicorn atlas.main:app</code> in <code>apps/api</code>, or open{" "}
            <a href="?mock=1" className="text-signal underline decoration-signal/40 underline-offset-2">
              simulated mode
            </a>
            .
          </p>
        )}
      </div>
    </div>
  );
}
