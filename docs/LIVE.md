# Phase 2: live agents

In Phase 2, missions run on the Claude API instead of scripted scenarios. The UI doesn't change its
contract: the live engine drives the same `WorldStore`, so every event in docs/EVENTS.md still applies.

## Modes

`POST /missions {objective, node, mode?, scenario_id?, speed?}`
- `mode: "live"` runs the live orchestrator. It requires `ANTHROPIC_API_KEY`, otherwise the endpoint returns 422.
- `mode: "simulated"` (or a `scenario_id` without a mode) plays a scenario, as in Phase 1.
- When `mode` is omitted, it defaults to live if a key is configured and no `scenario_id` is given, and to simulated otherwise.

`GET /config` returns `{ live_available: bool, models: {orchestrator, default}, web_search: bool, context_nodes: [node ids with local context] }`.

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

Errors: an LLM or API error is retried once with backoff. After that, the task goes FAILED, the agent goes ERROR,
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
system prompt as a cached block (prompt caching), truncated to `ATLAS_CONTEXT_MAX_CHARS` (default 60k) per agent.

## Models and cost

| Env | Default |
|---|---|
| `ATLAS_ORCHESTRATOR_MODEL` | `claude-opus-5-5` |
| `ATLAS_MODEL` | `claude-sonnet-5` |
| `ATLAS_FAST_MODEL` | `claude-haiku-4-5-20251001` (used for consultations and summaries) |

The mission's `usage` is updated after every LLM call and emitted as a `mission.updated` event
(`{mission}` payload; reducers upsert it like any other mission event). Cost is an **estimate** from a configurable price table
(`ATLAS_PRICES`, USD per million tokens).

## ATLAS protocol prompt (shared by every agent)

Every agent is told the following:
- It is a member of ATLAS, and it works on one task inside one mission and one node.
- Tools: `consult`, `request_approval`, `submit_report`, plus any role tools, such as web search for SOFIA
  when `ATLAS_WEB_SEARCH=1`.
- Never present an assumption as a fact. Tag every claim. Say what is unresolved.
- Never take or promise external actions without `request_approval`.
- Never reference information from outside the provided context and task inputs.
- Keep the working language the same as the objective's language.
