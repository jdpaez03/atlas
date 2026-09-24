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

**Phase 1 is done**: the full Command Center runs on simulated missions. Two scenarios are included:
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
uv run uvicorn atlas.main:app --reload

# Command Center → http://localhost:3000
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000, click **New mission**, pick a scenario and watch the team work.
Approve or reject when ALFRED asks. `ATLAS_SIM_SPEED=3` plays missions 3× faster.

**UI without the backend:** open http://localhost:3000/?mock=1 (add `&speed=3` to speed it up).

Copy `.env.example` to `.env` when Phase 2 needs an Anthropic API key.

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
