# Phase 2: live agents

In Phase 2, missions run on Claude (the API, or your Claude Max plan through Claude Code) instead of scripted scenarios. The UI doesn't change its
contract: the live engine drives the same `WorldStore`, so every event in docs/EVENTS.md still applies.

## Modes

`POST /missions {objective, node, mode?, scenario_id?, speed?}`
- `mode: "live"` runs the live orchestrator on the configured backend (see Backends). If no backend is usable,
  the endpoint returns 422 with the reason.
- `mode: "simulated"` (or a `scenario_id` without a mode) plays a scenario, as in Phase 1.
- When `mode` is omitted, it defaults to live if a backend is usable and no `scenario_id` is given, and to simulated otherwise.

`GET /config` returns `{ live_available: bool, backend: "api" | "subscription" | null, cost_basis: "api" |
"api_equivalent" | null, live_hint: string | null, models: {orchestrator, default, fast}, web_search: bool,
context_nodes: [node ids with local context] }`. `live_hint` says in plain words why live is unavailable
(e.g. "Claude Code CLI not found — install Claude Code and log in").

## Backends

`ATLAS_LLM_BACKEND` picks what runs live missions:

| Value | Runs on | Needs |
|---|---|---|
| `api` | Claude Messages API, billed per token | `ANTHROPIC_API_KEY` |
| `subscription` | your Claude Pro/Max plan: the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) drives the Claude Code CLI, which uses its own login | Claude Code logged in (`claude`, then `/login`) |
| `auto` (default) | `api` if `ANTHROPIC_API_KEY` is set, else `subscription` if the SDK and the CLI are usable, else live is off | |

The orchestrator's flow is shared (plan → DAG scheduling → follow-ups → consolidation, phases, statuses,
cancel). Only two things differ, behind `atlas/live/executor.py`: running one structured ATLAS step (plan,
follow-ups, mission report) and running one agent task.

**Subscription backend** (`atlas/live/sdk.py`). Each ATLAS step, agent task and consultation is one SDK session:
- The system prompt is the same text the api backend builds (role prompt + protocol + node context), passed
  as a file (Windows caps command lines at 32k characters). The user message is the same task message.
- Our tools are in-process SDK MCP tools (`mcp__atlas__consult`, `mcp__atlas__request_approval`,
  `mcp__atlas__submit_report`, and `create_plan` / `request_followups` / `submit_mission_report` for ATLAS).
  Their handlers run the same store code as the api backend. `consult` is a nested one-shot session with the
  target agent's role prompt on the fast model. `request_approval` waits for the human (`MCP_TOOL_TIMEOUT`
  is raised to 24 h for the CLI). A plan that fails validation goes back to ATLAS as a tool error in the
  same session (at most 2 attempts). `max_turns` = `ATLAS_MAX_TURNS`.
- **Safety.** The agents run on your computer, so every built-in Claude Code tool is removed (no Bash, Read,
  Write, Edit, Glob, Grep, Task/Agent, NotebookEdit, TodoWrite, Skill and so on: `tools=[]` plus an explicit
  deny list). The only built-ins allowed are `WebSearch` and `WebFetch`, and only for agents with the
  `research` or `market_study` capability (SOFIA, MERCATO) when web search is on. `permission_mode="dontAsk"`
  denies anything not pre-approved, so nothing ever prompts in a terminal and nothing can escalate.
  `setting_sources=[]` and `strict_mcp_config` keep your own CLAUDE.md, agents, skills, hooks, plugins and
  MCP servers out of ATLAS sessions; `--no-chrome` drops the browser integration. Prompts are delivered
  verbatim (no `@file` expansion), sessions run in an empty temp directory, and Claude Code writes no
  transcript of them (`--no-session-persistence`). `ANTHROPIC_API_KEY` is blanked for the CLI, so it
  always uses your plan login.
- **Models.** Defaults are the Claude Code aliases `opus` / `sonnet` / `haiku` (orchestrator / agents /
  consultations), unless `ATLAS_ORCHESTRATOR_MODEL` / `ATLAS_MODEL` / `ATLAS_FAST_MODEL` are set.
- **Web search** is on by default for research agents (it's included in the plan); `ATLAS_WEB_SEARCH=0`
  turns it off. Each search or fetch is a turn, so the default `ATLAS_MAX_TURNS` is 12 on this backend.
- **Usage.** Each session's tokens and the SDK's `total_cost_usd` go to the mission's usage. On a plan that
  cost is the **API-equivalent** value (what the tokens would cost on the API), not a bill:
  `cost_basis: "api_equivalent"`.
- **Plan limits.** When the plan's usage limit is hit (rate-limit event, `rate_limit` error or HTTP 429),
  the task goes FAILED with "Max plan usage limit reached — resets at …", the activity feed says so, no new
  work is started (tasks not yet started go FAILED with the same reason), the review is skipped and the
  mission closes with an automatic summary. A login problem fails the task with "Claude Code is not logged in".
  The CLI retries transient API errors itself; ATLAS does not add another retry on this backend.
- **Windows.** Claude Code runs as a subprocess, which needs asyncio's Proactor event loop. uvicorn uses a
  selector loop on Windows whenever `--reload` or `--workers` is on (uvicorn/loops/asyncio.py), and that
  loop cannot start subprocesses. Start the API with `uv run python -m atlas` (it passes a Proactor loop
  factory to uvicorn, `atlas/eventloop.py`). If it runs on a selector loop anyway, `/config` reports live
  as unavailable with that reason.

`POST /missions/{id}/cancel` stops a running mission (live or simulated). Its open tasks become CANCELLED and the mission is closed.

## Flow of a live mission

1. **OBJECTIVE → DECOMPOSITION.** ATLAS (orchestrator model) receives the objective, the node, the roster of
   agents allowed in that node (id, title, description, capabilities, and division if any) and the node
   context summary. It calls the tool `create_plan` with 2–8 tasks: `{ref, title, description,
   assigned_to, priority, depends_on[refs], requires_approval, approval_reason}`. The plan is
   validated: agents must exist and be allowed in the node, the refs must form a DAG, and there must be
   no cycles. If the plan is invalid, the validation errors go back to ATLAS once and it retries.
2. **DELEGATION.** Tasks are created in the store, and dependencies promote tasks to READY.
3. **EXECUTION.** The scheduler runs READY tasks concurrently, respecting each agent's
   `permissions.max_parallel_tasks` and a global cap (`ATLAS_MAX_CONCURRENCY`, default 4). Each task is
   one **agent run**, a tool-use loop of at most `ATLAS_MAX_TURNS` turns (default 8). The agent sees:
   - its role prompt (the YAML `system_prompt`, or the Claude Code `.md` body for `claude_md` agents)
   - the ATLAS protocol prompt (below)
   - the node context
   - the mission objective, its task, and the reports of the tasks it depends on (dependency outputs only;
     never data from other missions or nodes)
4. **COLLABORATION.** Tool `consult(agent_id, question)`: the target agent (which must be allowed in the node)
   answers with one LLM call using its own role prompt. This emits a REQUEST and an ANSWER message, and both
   agents show COLLABORATING. The limit is `ATLAS_MAX_CONSULTS` per task (default 2), and consultations can't nest.
5. **Human in the loop.** Tool `request_approval(reason, title, detail, proposed_action?)` pauses the agent
   (status WAITING) until the human decides in the UI. The decision and the note come back as the tool result.
   Agents use it for external actions, financial commitments, irreversible steps, ambiguous or conflicting
   information, and missing information. A human note is also how the user answers an agent's question.
   Tasks that the plan flagged with `requires_approval` must call it before they finish.
6. **Report.** Tool `submit_report(...)` produces an AgentReport, where every finding is a Claim tagged FACT /
   ASSUMPTION / SCENARIO / RECOMMENDATION. It ends the run. If an agent stops without submitting, its final
   text is wrapped into a low-confidence report.
7. **VALIDATION → CONSOLIDATION.** ATLAS reviews every report against the objective. It can open at most
   one round of up to 3 follow-up tasks (to fill gaps or resolve conflicts), and then calls
   `submit_mission_report(...)`.
8. **REPORTING → FOLLOW_UP → CLOSED.** The agents return to their default status.

Errors: an LLM or API error is retried once with backoff (api backend; on the subscription backend the CLI retries). After that, the task goes FAILED, the agent goes ERROR,
and the mission continues. The mission report lists the failures.

## Agent sources

| Adapter | Role prompt |
|---|---|
| `claude` | the `system_prompt` in the agent's YAML |
| `claude_md` | the body of the `.md` file at `adapter_config.path` (after `${ENV}` expansion). Its frontmatter `model: sonnet/opus/haiku/inherit` maps to the configured models. If the file is missing, the agent is **unavailable**: it's excluded from plans, and `GET /agents` still lists it. |
| `http` / `cli` / `mcp` | Phase 5 |

## Local context (never committed)

`ATLAS_LOCAL_DIR` (default: `../atlas-local`, next to the repo) holds private material:

```
atlas-local/
  context/
    corporate/*.md            loaded for every agent working a corporate mission
    corporate/eos/*.md        loaded only for EOS-division members (and ATLAS)
    personal/*.md             loaded only for personal missions
```

Node isolation applies here too: only the mission's node folder is ever read. Context goes into the
system prompt as a cached block (prompt caching), truncated to `ATLAS_CONTEXT_MAX_CHARS` (default 150k — about 40k tokens) per agent. When truncation happens it is logged.

## Models and cost

| Env | Default (api) | Default (subscription) |
|---|---|---|
| `ATLAS_ORCHESTRATOR_MODEL` | `claude-opus-5-5` | `opus` |
| `ATLAS_MODEL` | `claude-sonnet-5` | `sonnet` |
| `ATLAS_FAST_MODEL` | `claude-haiku-4-5-20251001` (used for consultations and summaries) | `haiku` |

The mission's `usage` is updated after every LLM call and emitted as a `mission.updated` event
(`{mission}` payload; reducers upsert it like any other mission event). On the api backend, cost is an **estimate** from a configurable price table
(`ATLAS_PRICES`, USD per million tokens). On the subscription backend it is the SDK's API-equivalent cost.

## ATLAS protocol prompt (shared by every agent)

Every agent is told the following:
- It is a member of ATLAS, and it works on one task inside one mission and one node.
- Tools: `consult`, `request_approval`, `submit_report`, plus any role tools, such as web search for SOFIA
  when `ATLAS_WEB_SEARCH=1` (on the subscription backend: WebSearch/WebFetch for research agents, on by default).
- Never present an assumption as a fact. Tag every claim. Say what is unresolved.
- Never take or promise external actions without `request_approval`.
- Never reference information from outside the provided context and task inputs.
- Keep the working language the same as the objective's language.
