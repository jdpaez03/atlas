# Event stream contract

The Command Center is a pure function of the event stream: `WorldState = fold(events)`.
The backend `WorldStore` and the frontend reducer apply **the same rules**, so a UI that
loads `GET /state` and then applies `WS /ws` events is always in sync.

## Transport

- `GET /state` → `WorldState` snapshot (includes `last_seq`).
- `WS /ws?since=<seq>` → replays events with `seq > since`, then streams live. Each frame is one `AtlasEvent` JSON.
- `GET /events?since=<seq>&mission_id=` → same events over HTTP.

## Payload rule

Every event carries the **full, updated object** under a fixed key. Reducers upsert by `id`
(or `agent_id` for agent states). No partial patches.

| `type` | payload | reducer action |
|---|---|---|
| `mission.created` | `{ mission: Mission }` | upsert `missions` |
| `mission.phase_changed` | `{ mission: Mission }` | upsert `missions` |
| `mission.updated` | `{ mission: Mission }` | upsert `missions` (usage/cost changes, live mode) |
| `mission.closed` | `{ mission: Mission }` | upsert `missions` |
| `task.created` | `{ task: Task }` | upsert `tasks` |
| `task.updated` | `{ task: Task }` | upsert `tasks` |
| `agent.state_changed` | `{ state: AgentState }` | upsert `agent_states` by `agent_id` |
| `message.sent` | `{ message: AgentMessage }` | append `messages` |
| `report.submitted` | `{ report: AgentReport }` | upsert `agent_reports` |
| `mission.report_ready` | `{ report: MissionReport }` | upsert `mission_reports` |
| `approval.requested` | `{ approval: ApprovalRequest }` | upsert `approvals` |
| `approval.decided` | `{ approval: ApprovalRequest }` | upsert `approvals` |
| `evidence.recorded` | `{ evidence: Evidence }` | upsert `evidence` |
| `log` | `{}` | activity feed only (`{reset: true, agent_states}` clears the world) |

Every event also has `summary` (one human line for the Activity Feed, e.g.
`"ATLAS assigned 'Market research' to SOFIA"`), `mission_id` and `agent_id` when relevant.

## Commands (HTTP)

| Method | Path | Body | Effect |
|---|---|---|---|
| `GET` | `/nodes` | – | enabled nodes |
| `GET` | `/scenarios` | – | available simulated missions `[{id, node, title, objective}]` |
| `POST` | `/missions` | `{ objective, node, scenario_id?, speed? }` | Phase 1: starts the scenario (default: first scenario of that node). Returns `Mission`. |
| `POST` | `/approvals/{id}/decision` | `{ decision: "APPROVED" \| "REJECTED", note? }` | Resolves a pending approval; a paused mission resumes. Returns `ApprovalRequest`. |
| `POST` | `/reset` | – | Clears missions **and the persisted history** (dev only). |

## History, attachments and the mission thread (Phase 3, docs/PHASE3.md B)

### Persistence

Every published event is appended to SQLite at `<ATLAS_LOCAL_DIR>/atlas.db` (WAL):

```sql
events(seq INTEGER PRIMARY KEY, type TEXT, mission_id TEXT, ts TEXT, json TEXT)  -- json = the AtlasEvent
```

On startup the events are folded back into the `WorldStore` and seed the bus, so `GET /state`,
`GET /events` and `WS /ws?since=` behave as if the server never stopped (seq keeps growing; events older
than the in-memory window are read from the db). Missions that were not `CLOSED` are then closed as
**interrupted**, with normal events: open tasks → `CANCELLED` (`task.updated`), pending approvals →
`EXPIRED` (`approval.decided`), a `log` line, then `mission.closed` with `mission.interrupted = true`, and busy
agents go back to their default status. A graceful shutdown does not close running missions; they come
back interrupted. `POST /reset` deletes the history too.

### Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/missions?node=&limit=50` | – | `MissionSummary[]`, newest first |
| `POST` | `/missions` | JSON (as above) **or** `multipart/form-data`: `objective`, `node`, `mode?`, `scenario_id?`, `speed?`, `files` (repeat the field; `files[]` also accepted) | `Mission` with `attachments` |
| `POST` | `/missions/{id}/attachments` | `multipart/form-data`: `files` / `files[]` | `Mission` (also emitted as `mission.updated`) |
| `GET` | `/missions/{id}/files/attachments/{name}` | – | the file (`Content-Disposition: attachment`) |
| `GET` | `/missions/{id}/files/outputs/{name}` | – | a deliverable from `outputs/<node>/<id>/` |
| `POST` | `/missions/{id}/messages` | `{ text }` | the human's `AgentMessage` (`from: "human"`, `to: "atlas"`, `type: REQUEST`) |

```ts
MissionSummary = { id, objective, node, mode: "simulated" | "live", phase: MissionPhase, round: number,
  interrupted: boolean, created_at, closed_at: string | null, report_versions: number[] /* e.g. [1, 2] */,
  usage: Usage }
```

**Uploads.** Files go to `<ATLAS_LOCAL_DIR>/missions/<id>/attachments/` under a safe name (no path parts;
characters outside `A-Za-z0-9._ -` become `_`; a clash becomes `name (2).ext`). Each one is an
`Attachment {id, name, kind: "file", uri: <local path>, size_bytes, download_url: "/missions/{id}/files/attachments/{name}"}`.
Limits: 25 MB per file (`413`), 20 files per mission (`422`); a failed upload stores nothing (and, on create,
creates no mission). On create the files are stored before the mission starts, so agents can read them from the first step.

**Downloads.** `{name}` must be a plain safe file name (`400` otherwise); the path must resolve inside the
mission's folder (symlinks escaping it → `404`). Unknown kind → `422`, unknown mission or file → `404`.
Use the `download_url` of attachments and deliverables as given.

**Thread.** The message is recorded as `message.sent` (`from: "human"`), then:
- *Live, running:* it becomes human guidance for every task that starts afterwards and for ATLAS's review and
  consolidation. ATLAS replies with an `ANSWER` (`from: "atlas"`, `to: "human"`, `in_reply_to` = the note).
- *Live, closed or interrupted:* a follow-up. ATLAS replies with an `ANSWER`; if it opens a round, the mission
  emits `mission.phase_changed` with `round: N` and `phase: DELEGATION` (summary `"Round N started · …"`,
  `interrupted` becomes false, `closed_at` null), new tasks carry `round: N`, the phases run
  `DELEGATION → EXECUTION → VALIDATION → CONSOLIDATION → REPORTING → FOLLOW_UP → CLOSED`, a new
  `MissionReport` with `version: N` arrives (`mission.final_report_id` points to it; older versions stay in
  `mission_reports`), and ATLAS posts a `RESULT` message ("Round N complete · report vN …"). `422` if no live
  backend is usable (the message is not recorded).
- *Simulated:* ATLAS answers "This is a simulated mission; follow-ups need a live mission."

Thread messages are the `messages` whose `from` or `to` is `"human"`.
