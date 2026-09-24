# ATLAS Contracts

Source of truth: [`apps/api/atlas/core/models.py`](../apps/api/atlas/core/models.py).
Generated: [`contracts/atlas.schema.json`](../contracts/atlas.schema.json) → [`apps/web/lib/contracts.ts`](../apps/web/lib/contracts.ts).
CI fails if the generated files are out of date.

## Core objects

| Object | What it is |
|---|---|
| `Mission` | A user objective and the phase it's in (OBJECTIVE → DECOMPOSITION → … → FOLLOW_UP → CLOSED). |
| `Task` | A unit of work: assignee, status, priority, `depends_on` (DAG), `requires_approval`, progress. |
| `AgentDefinition` | One YAML file in `/agents`: identity, adapter, capabilities, tools, permissions. |
| `AgentState` | Live status of an agent: `IDLE · WORKING · WAITING · COLLABORATING · REVIEWING · MONITORING · BLOCKED · COMPLETED · ERROR`. |
| `AgentMessage` | Structured agent-to-agent message: `from`, `to`, `type` (REQUEST/RESULT/ALERT/QUESTION/ANSWER/REVIEW), subject, body, attachments, confidence, `requires_response`. |
| `AgentReport` | What the agent was asked, did, used, found (`Claim`s tagged FACT/ASSUMPTION/SCENARIO/RECOMMENDATION), what's unresolved, confidence, limitations. |
| `MissionReport` | ATLAS's executive consolidation: summary, status, findings, conflicts, assumptions, human-attention items, next actions. |
| `ApprovalRequest` | A human intervention point with a reason (external comms, money, irreversible, ambiguous, insufficient info…). |
| `AtlasEvent` | Everything the UI sees. Each event has a typed `payload` and a human `summary` for the Activity Feed. |

## Event stream

`WS /ws` replays history, then streams live `AtlasEvent`s. `GET /events?since=<seq>` backfills after reconnects.

## External agents (bring your own)

Any existing agent can join the team by adding a YAML file with `kind: external` (see
`agents/_template.external.yaml`). ATLAS talks to it through one of three adapters:

| Adapter | Contract |
|---|---|
| `http` | ATLAS `POST`s a `Task` JSON to `adapter_config.url`; the agent responds with an `AgentReport` JSON. Works for webhooks, n8n, Make, Zapier, custom APIs. |
| `cli` | ATLAS runs `adapter_config.command` with the `Task` JSON on stdin; the agent prints an `AgentReport` JSON to stdout. Works for local Python scripts. |
| `mcp` | ATLAS connects to MCP server `adapter_config.server` and calls its `run_task` tool with the `Task`. |

External agents show up on the Agent Board, receive delegated tasks and appear in reports exactly like native ones.
