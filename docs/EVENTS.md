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
| `log` | `{}` | activity feed only |

Every event also has `summary` (one human line for the Activity Feed, e.g.
`"ATLAS assigned 'Market research' to SOFIA"`), `mission_id` and `agent_id` when relevant.

## Commands (HTTP)

| Method | Path | Body | Effect |
|---|---|---|---|
| `GET` | `/nodes` | – | enabled nodes |
| `GET` | `/scenarios` | – | available simulated missions `[{id, node, title, objective}]` |
| `POST` | `/missions` | `{ objective, node, scenario_id?, speed? }` | Phase 1: starts the scenario (default: first scenario of that node). Returns `Mission`. |
| `POST` | `/approvals/{id}/decision` | `{ decision: "APPROVED" \| "REJECTED", note? }` | Resolves a pending approval; a paused mission resumes. Returns `ApprovalRequest`. |
| `POST` | `/reset` | – | Clears missions (dev only). |
