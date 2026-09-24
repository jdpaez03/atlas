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
`agents/_template.external.yaml`). In live missions ATLAS talks to it through one of these adapters
(implementation: `apps/api/atlas/live/external.py`):

| Adapter | Status | How ATLAS calls it |
|---|---|---|
| `http` | live | `POST`s the request JSON to `adapter_config.url`; the response body is the report JSON. Works for webhooks, n8n, Make, Zapier, custom APIs. |
| `cli` | live | Runs `adapter_config.command` with the request JSON on stdin; the command prints the report JSON on stdout. Works for local scripts. |
| `claude_md` | live | A Claude Code agent file run on the Claude API as a native agent (see [LIVE.md](LIVE.md#agent-sources)). |
| `mcp` | not supported yet | The agent is listed but **unavailable** ("mcp adapter: not supported yet — use http or cli"). |

External agents show up on the Agent Board, are planned like native ones (the planner sees their title,
description and capabilities) and their reports feed dependent tasks and the mission report. An `http` agent
is available when its `url` expands to an `http(s)://` URL; a `cli` agent when its `command` is set.

### Request (ATLAS → agent), contract `atlas.external/1`

```json
{
  "contract": "atlas.external/1",
  "mission": {"id": "msn_…", "objective": "Evaluate the Polanco 2BR", "node": "corporate"},
  "task": {"id": "tsk_…", "title": "Count CRM deals", "description": "…", "priority": "MEDIUM",
           "requires_approval": false},
  "agent": {"id": "my-agent", "name": "MYAGENT"},
  "inputs": ["## Report · Market research · by SOFIA\nConfidence: HIGH\nFindings:\n- [FACT · HIGH] …"],
  "deadline_seconds": 300
}
```

`inputs` holds the rendered reports of the tasks this one depends on (and nothing else: no node context, no
other missions). `task.description` includes the human's attachments and guidance for the mission, if any.

### Response (agent → ATLAS)

```json
{
  "asked_to": "optional: what the agent understood it was asked",
  "actions_taken": ["Queried the CRM"],
  "inputs_used": ["CRM export 2026-09-24"],
  "findings": [
    {"kind": "FACT", "statement": "12 open deals", "sources": ["crm://deals?status=open"], "confidence": "HIGH"},
    {"kind": "ASSUMPTION", "statement": "…", "confidence": "MEDIUM"}
  ],
  "unresolved": [], "needs_agents": [], "confidence": "HIGH", "limitations": []
}
```

Parsing is tolerant: the object may be wrapped as `{"report": {…}}`; missing lists are empty; a finding may be a
plain string; an unknown `kind` becomes `ASSUMPTION` and an unknown `confidence` becomes `MEDIUM`. A `cli` agent
may log lines before the JSON as long as the JSON object is on the last line (logs belong on stderr). A response
that is not a JSON object, or has neither findings nor actions, is not an error: ATLAS wraps the raw output into
a LOW-confidence report that says so.

What ATLAS adds to every external report:
- Evidence `external_call` for the call itself (ref = the `url` / `command` **as written in the YAML**, so no
  expanded secrets; detail = HTTP status or exit code, duration, bytes; `ok: false` on failure).
- The limitation "External agent: its actions are self-reported (ATLAS recorded only the call)": ATLAS can't
  verify files or URLs the agent says it touched.
- Confidence is capped at `MEDIUM` unless every `FACT` finding has `sources`.

Failures (the task goes FAILED, the agent ERROR, the mission continues): HTTP non-2xx (status + a short body
excerpt), connection errors, timeouts (a `cli` process is killed), a non-zero exit code (stderr excerpt), a
command that can't start, or an unresolved `${VAR}`. Cancelling the mission kills a running `cli` process.

**Approval.** An external agent can't ask for approval mid-call. When the task has `requires_approval`, ATLAS
asks the human **before** calling it (the proposed action names the agent and its url/command) and only calls
it if approved. If rejected, the agent is not called and the task's report says it was not executed.

### Adapter config

| Key | Adapter | Meaning |
|---|---|---|
| `url` | http | Endpoint that receives the `POST`. `${VAR}` expanded. |
| `headers` | http | Map of extra headers, e.g. `Authorization: "Bearer ${MY_TOKEN}"`. Values `${VAR}` expanded. |
| `command` | cli | Command line, split like a POSIX shell (quotes work) but **never run through a shell**: no pipes, redirects or globbing. `${VAR}` is expanded per argument, so paths with spaces stay whole. On Windows the split keeps backslashes. |
| `cwd` | cli | Working directory (`${VAR}` and `~` expanded). Default: the ATLAS API's working directory. |
| `env` | cli | Map of extra environment variables for the process (values `${VAR}` expanded). |
| `inherit_env` | cli | `true` (default): the process gets ATLAS's environment plus `env`. `false`: only PATH/HOME/TEMP-like variables plus `env` (keeps e.g. `ANTHROPIC_API_KEY` away from the script). |
| `timeout_seconds` | both | Deadline for the whole call (default 300); also sent as `deadline_seconds`. |

Built-in variables (overridable from the environment / `.env`): `${ATLAS_PYTHON}` = the Python running ATLAS,
`${ATLAS_REPO_DIR}` = the repo checkout. Any other `${VAR}` must be set, or the call fails (and an `http` agent
with an unresolved `url` is unavailable).

### Security

- **Secrets come from the environment**, never from YAML: reference them as `${NAME}` in `url`, `headers` or
  `env`. Evidence and the activity feed show the unexpanded value.
- ATLAS runs **only** the `command` / `url` in your YAML files. Nothing in a task, a report or a model output can
  change what is executed or called; the planner only chooses *which* configured agent gets a task.
- `cli` commands run as the same OS user as the ATLAS API, with its permissions. Treat adding a `cli` agent
  like installing a script you run yourself, and use `inherit_env: false` for scripts you don't fully trust.
- An `http` agent receives the mission objective, the task and its dependency reports. Only point it at
  endpoints you'd share that material with.

### Example: ECHO (`cli`)

[`examples/external-agents/echo_agent.py`](../examples/external-agents/echo_agent.py) (standard library only)
reads the request and returns one FACT with the word counts of the task description and the inputs, plus one
ASSUMPTION. [`agents/_example.cli.yaml`](../agents/_example.cli.yaml) wires it up:

```yaml
adapter: cli
adapter_config:
  command: ${ATLAS_PYTHON} ${ATLAS_REPO_DIR}/examples/external-agents/echo_agent.py
  timeout_seconds: 30
```

Files starting with `_` are ignored by the registry. To try it, copy it to `agents/echo.yaml`, set
`enabled: true` and restart the API; ECHO then appears on the Agent Board and can be planned. Test the script
by hand with
`echo '{"task": {"title": "t", "description": "count these words"}, "inputs": []}' | python examples/external-agents/echo_agent.py`.
