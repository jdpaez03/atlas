# ATLAS — Multi-Agent AI Command Center

**One Intelligence. Many Agents.**

ATLAS is a command center for a team of specialized AI agents. Give it an objective. ATLAS breaks it
into tasks, delegates them to specialists, supervises their collaboration and delivers a consolidated
report. You stay the director: every consequential action waits for your approval.

| Agent | Role |
|---|---|
| **ATLAS** | Orchestrator: decomposes, delegates, supervises, consolidates |
| **SOFIA** | Research: sources, markets, context |
| **ARGOS** | Monitor: changes, anomalies, alerts |
| **ORACLE** | Strategy: scenarios and analysis, with facts kept apart from assumptions |
| **ALFRED** | Execution: deliverables and operations, with approval gates |

Agents are YAML files in [`/agents`](agents). You can add, remove or swap them, including **existing
agents you already run** (via http, cli or mcp adapters). See [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Status

**Inbox follow-ups** (new): ATLAS can read your work Outlook and keep a follow-up board with drafted reminders — see [docs/INBOX_SETUP.md](docs/INBOX_SETUP.md). **Phase 3 is done**: agents can read your files (sandboxed, per node), take attachments, write real deliverables, and every action they take is recorded as evidence; each mission has a follow-up thread and history survives restarts. **Phase 2**: live missions run on Claude, either on your **Claude Max/Pro plan** (through Claude Code, no API key) or on an API key. Simulated missions still work without either. Two scenarios are included:
a real-estate investment analysis, and an EOS quarterly Rocks diagnosis run by the EOS division.
See the [roadmap](docs/ROADMAP.md).

Agents are grouped into **nodes** (Corporate, Personal…), which are isolated contexts. Node-bound agents
never leave their node. Shared agents can serve every node but never carry context between them.

## Run locally

Requirements: Python 3.11+ with [uv](https://docs.astral.sh/uv/), and Node 20+.

```bash
# API  → http://localhost:8000  (docs at /docs)
cd apps/api
uv sync
uv run python -m atlas            # add --reload while developing

# Command Center → http://localhost:3000
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000, click **New mission**, pick a scenario and watch the team work.
Approve or reject when ALFRED asks. `ATLAS_SIM_SPEED=3` plays missions 3× faster.

**UI without the backend:** open http://localhost:3000/?mock=1 (add `&speed=3` to speed it up).

Copy `.env.example` to `.env` to configure live agents (Phase 2).

## Live agents on your Claude Max plan

Live missions can run on your Claude Pro/Max subscription instead of an API key, through the
[Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/), which drives Claude Code with its own login.
Agent SDK usage draws from your plan's usage limits (see Anthropic's help center).

1. Install Claude Code and log in once: run `claude` and type `/login` with your Max account.
2. In `.env`, leave `ANTHROPIC_API_KEY` empty (or set `ATLAS_LLM_BACKEND=subscription`, which ignores the key).
3. `cd apps/api && uv sync && uv run python -m atlas`

The mission panel shows **Max plan · API-equivalent cost**: the cost figure is what the tokens would cost on
the API, not a bill. If the plan's usage limit is hit, the running task fails with "Max plan usage limit
reached — resets at …" and ATLAS closes the mission with what it has. Agents get no file, shell or other
local tools; research agents get web search and fetch. Details: [docs/LIVE.md](docs/LIVE.md#backends).

**Windows, quickest:** from the repo folder run `powershell -ExecutionPolicy Bypass -File .\start.ps1`. It opens the API and the Command Center in two windows and then your browser.

**Windows, by hand** (PowerShell):

```powershell
cd apps\api
uv sync
uv run python -m atlas        # not `uvicorn --reload`: its selector event loop can't start Claude Code
```

`python -m atlas` gives uvicorn a Proactor event loop, which Claude Code needs. `--reload` is accepted and
keeps that loop, but it's the least-tested combination on Windows: if live agents misbehave, drop `--reload`. Claude Code must be the native `claude.exe` (the SDK bundles one; npm's `claude.cmd` shim
is not supported). Set `ATLAS_CLAUDE_CLI` to use a specific `claude.exe`.

## Repo layout

```
agents/          agent definitions (one YAML per agent)
apps/api/        FastAPI backend: contracts, registry, event bus, orchestrator
apps/web/        Next.js Command Center
contracts/       generated JSON Schema (shared by api and web)
docs/            roadmap, contracts, architecture notes
scripts/         codegen and utilities
```

## Changing a contract

```bash
cd apps/api && uv run python ../../scripts/export_schema.py
cd ../web   && npm run gen:types
```

## License

MIT
