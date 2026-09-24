"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { NO_LIVE_CONFIG, api, argosApi, inboxApi, wsUrl, type ArgosApi, type InboxApi, type AtlasConfig, type Availability, type Decision, type LaunchMissionBody, type MissionSummary, type Scenario } from "./api";
import type {
  AgentMessage,
  AgentReport,
  AgentState,
  Alert,
  ApprovalRequest,
  AtlasEvent,
  Brief,
  Digest,
  EmailDraft,
  Evidence,
  FollowUp,
  Mission,
  MissionReport,
  RockStatus,
  Task,
  WorldState,
} from "./contracts";

/* ------------------------------------------------------------------------------------------------
 * Reducer — mirrors docs/EVENTS.md exactly: WorldState = fold(events).
 * Full objects are upserted by id (agent_states by agent_id); messages append, deduped by id.
 * ---------------------------------------------------------------------------------------------- */

export function emptyWorld(): WorldState {
  return {
    nodes: [],
    divisions: [],
    agents: [],
    agent_states: [],
    missions: [],
    tasks: [],
    messages: [],
    agent_reports: [],
    mission_reports: [],
    approvals: [],
    evidence: [],
    followups: [],
    drafts: [],
    digests: [],
    alerts: [],
    rocks: [],
    briefs: [],
    last_seq: 0,
  };
}

function upsertBy<T>(list: T[], item: T | undefined, key: (x: T) => string): T[] {
  if (!item) return list;
  const k = key(item);
  const i = list.findIndex((x) => key(x) === k);
  if (i === -1) return [...list, item];
  const next = list.slice();
  next[i] = item;
  return next;
}
const byId = <T extends { id: string }>(x: T) => x.id;

export function applyEvent(s: WorldState, e: AtlasEvent): WorldState {
  // Already folded into the snapshot (or replayed twice after a reconnect).
  if (typeof e.seq === "number" && e.seq <= s.last_seq) return s;
  const p = (e.payload ?? {}) as Record<string, unknown>;
  const next: WorldState = { ...s, last_seq: Math.max(s.last_seq, e.seq ?? s.last_seq) };
  // Reset (dev): clears the world and replaces agent states. Mirrors apply_event() in core/store.py.
  if (e.type === "log" && p.reset) {
    return {
      ...next,
      missions: [], tasks: [], messages: [], agent_reports: [], mission_reports: [], approvals: [], evidence: [],
      agent_states: (p.agent_states as AgentState[] | undefined) ?? s.agent_states,
    };
  }
  // Generic rule (docs/EVENTS.md): upsert every full object the event carries under a known key.
  // Some events carry two objects (task.created → task + mission; report.submitted → report + task).
  if (p.mission) next.missions = upsertBy(s.missions, p.mission as Mission, byId);
  if (p.task) next.tasks = upsertBy(s.tasks, p.task as Task, byId);
  if (p.state) next.agent_states = upsertBy(s.agent_states, p.state as AgentState, (x) => x.agent_id);
  if (p.evidence) next.evidence = upsertBy(s.evidence ?? [], p.evidence as Evidence, byId);
  if (p.followup) next.followups = upsertBy(s.followups ?? [], p.followup as FollowUp, byId);
  if (p.digest) next.digests = upsertBy(s.digests ?? [], p.digest as Digest, byId);
  if (p.draft) next.drafts = upsertBy(s.drafts ?? [], p.draft as EmailDraft, byId);
  if (p.alert) next.alerts = upsertBy(s.alerts ?? [], p.alert as Alert, byId);
  if (p.rock) next.rocks = upsertBy(s.rocks ?? [], p.rock as RockStatus, byId);
  if (p.brief) next.briefs = upsertBy(s.briefs ?? [], p.brief as Brief, byId);
  if (p.approval) next.approvals = upsertBy(s.approvals, p.approval as ApprovalRequest, byId);
  if (p.message) {
    const m = p.message as AgentMessage;
    if (!s.messages.some((x) => x.id === m.id)) next.messages = [...s.messages, m];
  }
  if (p.report) {
    if (e.type === "mission.report_ready") {
      next.mission_reports = upsertBy(s.mission_reports, p.report as MissionReport, byId);
    } else {
      next.agent_reports = upsertBy(s.agent_reports, p.report as AgentReport, byId);
    }
  }
  return next;
}

/* ------------------------------------------------------------------------------------------------
 * Transport — live (HTTP + WS) or mock (scripted, client-side). Both feed the same reducer.
 * ---------------------------------------------------------------------------------------------- */

export type ConnStatus = "connecting" | "live" | "reconnecting" | "offline" | "mock";

export interface TransportHandlers {
  onSnapshot(w: WorldState): void;
  onEvent(e: AtlasEvent): void;
  /** Recent events used only to seed the Activity Feed (already folded into the snapshot). */
  onBackfill(events: AtlasEvent[]): void;
  onStatus(s: ConnStatus): void;
}

export interface Transport {
  mode: "live" | "mock";
  connect(h: TransportHandlers): () => void;
  scenarios(): Promise<Scenario[]>;
  launch(body: LaunchMissionBody, files?: File[]): Promise<Mission>;
  /** POST /missions/{id}/messages — a note from the human to ATLAS (mission thread). */
  sendMessage(missionId: string, text: string): Promise<unknown>;
  /** POST /missions/{id}/attachments */
  attach(missionId: string, files: File[]): Promise<unknown>;
  /** GET /missions?node= — null when the backend has no history endpoint. */
  missions(node: string | null): Promise<MissionSummary[] | null>;
  decide(id: string, decision: Decision, note?: string): Promise<ApprovalRequest>;
  cancel(id: string): Promise<Mission>;
  config(): Promise<AtlasConfig>;
  availability(): Promise<Availability>;
  /** Inbox follow-ups & drafts (docs/INBOX.md). */
  inbox: InboxApi;
  /** ARGOS monitoring: alerts, Rocks, briefs (docs/ARGOS.md). */
  argos: ArgosApi;
}

export function liveTransport(): Transport {
  return {
    mode: "live",
    scenarios: api.scenarios,
    launch: api.launch,
    decide: api.decide,
    cancel: api.cancel,
    sendMessage: api.sendMessage,
    attach: api.attach,
    missions: (node) => api.missions(node),
    config: api.config,
    availability: api.availability,
    inbox: inboxApi,
    argos: argosApi,
    connect(h) {
      let closed = false;
      let ws: WebSocket | null = null;
      let timer: ReturnType<typeof setTimeout> | null = null;
      let lastSeq = -1; // -1 = no snapshot yet
      let attempt = 0;

      const schedule = (fn: () => void) => {
        const delay = Math.min(8000, 800 * 2 ** attempt++);
        timer = setTimeout(fn, delay);
      };

      const openSocket = () => {
        if (closed) return;
        ws = new WebSocket(wsUrl(lastSeq));
        ws.onopen = () => {
          attempt = 0;
          h.onStatus("live");
        };
        ws.onmessage = (msg) => {
          try {
            const e = JSON.parse(typeof msg.data === "string" ? msg.data : "") as AtlasEvent;
            if (!e || typeof e.type !== "string") return;
            if (typeof e.seq === "number") lastSeq = Math.max(lastSeq, e.seq);
            h.onEvent(e);
          } catch {
            /* ignore malformed frame */
          }
        };
        ws.onclose = () => {
          ws = null;
          if (closed) return;
          h.onStatus("reconnecting");
          schedule(openSocket);
        };
        ws.onerror = () => ws?.close();
      };

      const boot = async () => {
        if (closed) return;
        h.onStatus(attempt === 0 ? "connecting" : "offline");
        try {
          const snap = await api.state();
          if (closed) return;
          lastSeq = snap.last_seq ?? 0;
          h.onSnapshot(snap);
          api
            .events(Math.max(0, lastSeq - 200))
            .then((evts) => !closed && h.onBackfill(evts))
            .catch(() => undefined);
          openSocket();
        } catch {
          if (closed) return;
          h.onStatus("offline");
          schedule(boot);
        }
      };

      void boot();
      return () => {
        closed = true;
        if (timer) clearTimeout(timer);
        ws?.close();
      };
    },
  };
}

/* ------------------------------------------------------------------------------------------------
 * useAtlas()
 * ---------------------------------------------------------------------------------------------- */

const FEED_LIMIT = 400;

interface StoreState {
  world: WorldState;
  feed: AtlasEvent[]; // newest first
  loaded: boolean;
}

type Action =
  | { type: "snapshot"; world: WorldState }
  | { type: "event"; event: AtlasEvent }
  | { type: "backfill"; events: AtlasEvent[] };

function mergeFeed(feed: AtlasEvent[], events: AtlasEvent[]): AtlasEvent[] {
  const seen = new Set(feed.map((e) => e.id));
  const merged = [...feed, ...events.filter((e) => !seen.has(e.id))];
  merged.sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0));
  return merged.slice(0, FEED_LIMIT);
}

function storeReducer(s: StoreState, a: Action): StoreState {
  switch (a.type) {
    case "snapshot":
      // Tolerate a pre-Phase-3 snapshot without `evidence`.
      return { ...s, world: { ...a.world, evidence: a.world.evidence ?? [], followups: a.world.followups ?? [], drafts: a.world.drafts ?? [], digests: a.world.digests ?? [], alerts: a.world.alerts ?? [], rocks: a.world.rocks ?? [], briefs: a.world.briefs ?? [] }, loaded: true };
    case "event":
      return { ...s, world: applyEvent(s.world, a.event), feed: mergeFeed(s.feed, [a.event]) };
    case "backfill":
      return { ...s, feed: mergeFeed(s.feed, a.events) };
  }
}

function detectMock(): boolean {
  if (process.env.NEXT_PUBLIC_ATLAS_MOCK === "1") return true;
  if (typeof window === "undefined") return false;
  const q = new URLSearchParams(window.location.search).get("mock");
  return q === "1" || q === "true";
}

export function useAtlas() {
  const [state, dispatch] = useReducer(storeReducer, { world: emptyWorld(), feed: [], loaded: false });
  const [conn, setConn] = useState<ConnStatus>("connecting");
  const [transport, setTransport] = useState<Transport | null>(null);
  const transportRef = useRef<Transport | null>(null);

  useEffect(() => {
    let cancelled = false;
    let disconnect: (() => void) | null = null;
    (async () => {
      let t: Transport;
      if (detectMock()) {
        const { mockTransport } = await import("./mock");
        const speed = Number(new URLSearchParams(window.location.search).get("speed") ?? "1");
        t = mockTransport({ speed: Number.isFinite(speed) && speed > 0 ? speed : 1 });
      } else {
        t = liveTransport();
      }
      if (cancelled) return;
      transportRef.current = t;
      setTransport(t);
      disconnect = t.connect({
        onSnapshot: (world) => dispatch({ type: "snapshot", world }),
        onEvent: (event) => dispatch({ type: "event", event }),
        onBackfill: (events) => dispatch({ type: "backfill", events }),
        onStatus: setConn,
      });
    })();
    return () => {
      cancelled = true;
      disconnect?.();
    };
  }, []);

  const launch = useCallback((body: LaunchMissionBody, files?: File[]) => {
    const t = transportRef.current;
    if (!t) return Promise.reject(new Error("not connected"));
    return t.launch(body, files);
  }, []);
  const sendMessage = useCallback((missionId: string, text: string) => {
    const t = transportRef.current;
    if (!t) return Promise.reject(new Error("not connected"));
    return t.sendMessage(missionId, text);
  }, []);
  const attach = useCallback((missionId: string, files: File[]) => {
    const t = transportRef.current;
    if (!t) return Promise.reject(new Error("not connected"));
    return t.attach(missionId, files);
  }, []);
  const missionHistory = useCallback((node: string | null) => {
    const t = transportRef.current;
    if (!t) return Promise.resolve(null);
    return t.missions(node).catch(() => null);
  }, []);
  const decide = useCallback((id: string, decision: Decision, note?: string) => {
    const t = transportRef.current;
    if (!t) return Promise.reject(new Error("not connected"));
    return t.decide(id, decision, note);
  }, []);
  const cancel = useCallback((id: string) => {
    const t = transportRef.current;
    if (!t) return Promise.reject(new Error("not connected"));
    return t.cancel(id);
  }, []);
  const config = useCallback(() => {
    const t = transportRef.current;
    if (!t) return Promise.resolve(NO_LIVE_CONFIG);
    return t.config().catch(() => NO_LIVE_CONFIG);
  }, []);
  const availability = useCallback(() => {
    const t = transportRef.current;
    if (!t) return Promise.resolve({} as Availability);
    return t.availability().catch(() => ({}) as Availability);
  }, []);
  const scenarios = useCallback(() => {
    const t = transportRef.current;
    if (!t) return Promise.resolve([] as Scenario[]);
    return t.scenarios();
  }, []);

  // Inbox: stable wrappers that route to whichever transport is active.
  const inbox = useMemo<InboxApi>(() => {
    const t = () => {
      const x = transportRef.current;
      if (!x) throw new Error("not connected");
      return x.inbox;
    };
    return {
      status: () => (transportRef.current ? transportRef.current.inbox.status().catch(() => null) : Promise.resolve(null)),
      scan: () => t().scan(),
      connect: () => t().connect(),
      connectStatus: () => t().connectStatus(),
      patchFollowup: (id, p) => t().patchFollowup(id, p),
      draftFollowup: (id) => t().draftFollowup(id),
      decideDraft: (id, b) => t().decideDraft(id, b),
      runDigest: (days) => t().runDigest(days),
    };
  }, []);

  // ARGOS: same pattern as the inbox wrappers.
  const argos = useMemo<ArgosApi>(() => {
    const t = () => {
      const x = transportRef.current;
      if (!x) throw new Error("not connected");
      return x.argos;
    };
    return {
      status: () => (transportRef.current ? transportRef.current.argos.status().catch(() => null) : Promise.resolve(null)),
      run: (checks) => t().run(checks),
      patchAlert: (id, st) => t().patchAlert(id, st),
      reloadRocks: () => t().reloadRocks(),
      patchRock: (id, current) => t().patchRock(id, current),
      buildBrief: () => t().buildBrief(),
      briefs: () => t().briefs(),
      briefFileUrl: (b) => transportRef.current?.argos.briefFileUrl(b),
      suiteConnect: () => t().suiteConnect(),
      suiteConnectStatus: () => t().suiteConnectStatus(),
    };
  }, []);

  return useMemo(
    () => ({
      world: state.world,
      feed: state.feed,
      loaded: state.loaded,
      conn,
      mode: transport?.mode ?? null,
      ready: transport !== null,
      launch,
      decide,
      scenarios,
      cancel,
      config,
      availability,
      sendMessage,
      attach,
      missionHistory,
      inbox,
      argos,
    }),
    [state, conn, transport, launch, decide, scenarios, cancel, config, availability, sendMessage, attach, missionHistory, inbox, argos],
  );
}

export type AtlasStore = ReturnType<typeof useAtlas>;
