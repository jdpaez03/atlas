# ATLAS Roadmap

Principle: **contracts before code.** Everything in `apps/api/atlas/core/models.py` is the shared
language between the orchestrator, the agents and the Command Center. Once it is stable, builders
work in parallel against it without colliding.

| Phase | Scope | Demo at the end | Status |
|---|---|---|---|
| **0 · Contracts** | Data models (Mission, Task, AgentStatus, AgentMessage, AgentReport, MissionReport, ApprovalRequest, AtlasEvent), agent registry with external-agent adapters, event bus + WebSocket, JSON Schema → TypeScript generation, CI | API + Agent Board reading the live registry | ✅ done |
| **1 · Command Center (simulated)** | Mission panel, Agent Board with live statuses, Task Board with dependencies, collaboration graph, activity feed, reports view, human-intervention queue. A scripted "real-estate investment" mission replays through the real event stream (mock adapter). | Full experience, clickable, fake agents | ✅ done |
| **2 · Live orchestrator** | ATLAS decomposes objectives with Claude into a task DAG, delegates, runs SOFIA/ORACLE/ALFRED on the Claude API, structured messages between agents | Type an objective → real agents work live | ✅ done (Max plan or API key) |
| **3 · Files, evidence, thread, history** | Sandboxed read access to your folders per node, attachments, real deliverables (md/csv/xlsx/docx), system-recorded evidence + unverified-claim check, mission thread with follow-up rounds and report versions, SQLite history that survives restarts | Attach a file → agents read it, write a deliverable, prove it; follow up in the thread | ✅ done |
| **4 · Tools & live data** | Web search (SOFIA/MERCATO), deliverables, Inbox (follow-ups, drafts, CC digest), ARGOS monitoring (dashboards from email week-over-week, L10 via PAGA Suite, Rocks rules, Monday L10 brief) | Real information in, real deliverables out, alerts with verbatim evidence | ✅ built · L10 + Rocks live from PAGA Suite |
| **5 · Modularity & quality** | External agents (http / mcp / cli adapters) live, AUDITOR agent, cost/token tracking, retries & error recovery | Add an agent = add a YAML file | ✅ built (AUDITOR, retries/resume, usage by agent, http/cli agents) |

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
- 2026-09-23 · **Phase 2 done**: live agents on the Claude API (plan → parallel tasks → consult → approvals →
  reports → follow-ups → consolidation), with usage/cost tracking and cancel. Verified end-to-end with a scripted
  model; first real-API run pending the key.
- 2026-09-23 · **MERCATO** (Corporate): the user's `estudio-mercado-vivienda-vertical` skill joins as the market-
  studies agent via `claude_md`; its prompt lives in `atlas-local/agents/` (private).
- 2026-09-24 · **Max plan backend**: live missions run on the user's Claude Max plan through the Claude Agent SDK
  (Claude Code), locked down: no local tools except ATLAS's own sandboxed ones, no user settings loaded, no transcripts.
- 2026-09-24 · **Phase 3 re-scoped from first real use**: agents had no file access and described actions they never
  took → sandboxed file tools per node, attachments, deliverables, and *evidence written by the system, not the agent*
  (reports carry it; unverified claims are flagged). Plus a mission thread (follow-up rounds, report versions) and
  persistent history. Corporate agents read `~/Documents` by default (`ATLAS_FILE_ROOTS_CORPORATE`).
- 2026-09-24 · **Inbox (Phase 4 slice)**: HERMES reads the work Outlook mailbox (Microsoft Graph device-code sign-in,
  or a Power Automate → OneDrive folder when IT approval isn't available), turns commitments into follow-ups on the
  ATLAS board, and ALFRED drafts follow-ups. ATLAS never sends: approved drafts go to Outlook Drafts or an `.eml`.
  Email bodies are never stored (only sender, subject, date and a verbatim excerpt). Setup: docs/INBOX_SETUP.md.
- 2026-09-24 · **ARGOS monitoring**: dashboards arrive as email attachments → saved privately, compared week over week
  (identical/missing reports, moved dates, removed rows, KPI mismatches) with verbatim quotes from both weeks; L10 to-dos
  and issues via the PAGA Suite API (read-only); Rocks from a private rocks.yaml with the dossier's rules (overdue = failed,
  pace < 50% = at risk, two owners = no owner, no measurable = finding); Monday 07:30 L10 brief (.docx). A brief never
  says "all clear" for a source it couldn't check.
- 2026-09-24 · **PAGA Suite connection**: the Suite's sessions are its own opaque tokens (not Entra JWTs), so ATLAS reads
  it with a read-only key (`ATLAS_API_KEY` in the Suite, GET-only allowlist: L10 to-dos/issues/junta/resumen and
  `/rocks`). The Suite got its own **Rocks module** (dueño único, medible obligatorio, ritmo < 50% = off-track
  automático, off-track → issue al IDS, calificación binaria y cierre de trimestre); ARGOS reads Rocks from it
  instead of rocks.yaml when the Suite is configured.
- 2026-09-24 · **Phase 5**: AUDITOR (shared, never planned) checks every report against the system's evidence after
  each round, with deterministic prechecks, PASS / ISSUES / FAIL verdicts, one revision round for a FAIL, and a
  system check that every figure in the executive report traces to an agent report (docs/AUDITOR.md). Transient task
  failures are retried with backoff, and a closed mission's failed or cancelled tasks can be resumed as a new round.
  Usage is split by agent (`GET /usage`). External agents run live via `http` and `cli` (contract
  `atlas.external/1`, docs/CONTRACTS.md); `mcp` stays unavailable. Deployment on the home PC runs natively on Windows
  (service + Tailscale), not in Docker: ATLAS depends on the user's Claude Code login, the DPAPI-encrypted Outlook
  token cache and local/OneDrive files.
- 2026-09-24 · **Agent browser**: agents that need a signed-in site with no API get a `browser:` block and their
  own **dedicated** Playwright profile (installed Chrome, `atlas-local/browser/<profile>`). They never get the
  user's everyday Chrome, which stays off (`--no-chrome`). The human signs in once (`atlas-browser login`). The
  tools cover open, snapshot, click, type, select, tables and download. Guard rails are enforced in code:
  allowed_domains only, no password typing, and approval + confirm before a consequential click (delete / buy /
  send / credits / log out). Downloads and saved tables land in the mission outputs as evidence (docs/BROWSER.md).
  MERCATO is the first agent to get one.
- 2026-09-24 · **SCRIBE (institutional documents)**: after consolidation, a non-planned agent writes a structured
  document spec and the system renders it with the node's private brand kit into a PDF report and an
  editable committee deck (native tables and charts, speaker notes). Figures in the documents go through the
  same traceability check as the executive report. Each mission can opt out. PAGA's kit comes from its
  official PowerPoint template (PAGA TEMPLATE.potm) and institutional presentation: #002A53 navy, light
  thin type, italic accent words, a vertical side label (docs/PUBLISHING.md).
- 2026-09-25 · **Briefs through SCRIBE**: the Monday L10 brief becomes a branded PDF plus a deck to project in
  the meeting, and the CC digest becomes a branded PDF. Both are laid out from their already-validated content
  without another model call. The L10 brief's .docx stays.
