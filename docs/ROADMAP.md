# ATLAS Roadmap

Principle: **contracts before code.** Everything in `apps/api/atlas/core/models.py` is the shared
language between the orchestrator, the agents and the Command Center. Once it is stable, builders
work in parallel against it without colliding.

| Phase | Scope | Demo at the end | Status |
|---|---|---|---|
| **0 · Contracts** | Data models (Mission, Task, AgentStatus, AgentMessage, AgentReport, MissionReport, ApprovalRequest, AtlasEvent), agent registry with external-agent adapters, event bus + WebSocket, JSON Schema → TypeScript generation, CI | API + Agent Board reading the live registry | ✅ done |
| **1 · Command Center (simulated)** | Mission panel, Agent Board with live statuses, Task Board with dependencies, collaboration graph, activity feed, reports view, human-intervention queue. A scripted "real-estate investment" mission replays through the real event stream (mock adapter). | Full experience, clickable, fake agents | next |
| **2 · Live orchestrator** | ATLAS decomposes objectives with Claude into a task DAG, delegates, runs SOFIA/ORACLE/ALFRED on the Claude API, structured messages between agents | Type an objective → real agents work live | needs API key |
| **3 · Human-in-the-loop & reporting** | Approval gates (external comms, money, irreversible, conflicting info), agent reports, ATLAS executive report, persistent mission history (SQLite) | Approve/reject from UI; finished missions produce reports | |
| **4 · Tools** | SOFIA web search/fetch, ARGOS scheduled checks + alerts, ALFRED document/spreadsheet generation | Real information in, real deliverables out | |
| **5 · Modularity & quality** | External agents (http / mcp / cli adapters) live, AUDITOR agent, cost/token tracking, retries & error recovery | Add an agent = add a YAML file | |

## Parallel build plan (Phase 1 onward)

- **Builder A — UI:** Command Center components against the event stream.
- **Builder B — Engine:** orchestrator, task DAG, event bus, API routes.
- **Builder C — Agents:** role prompts, tools, adapters (native + external).
- **Builder D — Test missions:** scripted and live scenarios for end-to-end checks.
- **Lead (Claude):** architecture, integration, review, phase demos.

## Decisions log

- 2026-09-23 · Stack: Next.js 15 + Tailwind 4 (web), FastAPI + Pydantic (api), Claude API (agents), SQLite → Postgres.
- 2026-09-23 · Added `MONITORING` agent status (ARGOS' steady state; used in the spec's board example).
- 2026-09-23 · External agents are first-class: `kind: external` + `http | mcp | cli` adapter. Existing agents
  can be plugged in early and kept or dropped later.
- 2026-09-23 · Anthropic API key lives only in ATLAS's `.env` (never a global env var, which would make
  Claude Code bill the key instead of the Max plan).
- 2026-09-23 · **Nodes**: missions live in one node (Corporate, Personal…). Node-bound agents never leave their
  node; shared core agents serve all nodes without carrying context across. Personal node structure TBD.
- 2026-09-23 · **EOS division** (Corporate): the six existing Claude Code EOS agents (Vision, People, Data, Issues,
  Process, Traction) join via the `claude_md` adapter. Their prompts stay on the local machine
  (`ATLAS_CLAUDE_AGENTS_DIR`), never in the public repo.
