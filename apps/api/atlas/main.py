"""ATLAS API.

  GET  /health                      liveness
  GET  /agents                      agent registry (definitions)
  GET  /nodes                       enabled nodes
  GET  /scenarios                   simulated missions [{id, node, title, objective}]
  GET  /state                       WorldState snapshot (with last_seq)
  GET  /events?since=&mission_id=   event history
  GET  /config                      {live_available, backend, cost_basis, live_hint, models, web_search,
                                     context_nodes}
  GET  /agents/availability         {agent_id: {available, reason?}} for live missions
  GET  /missions?node=&limit=       mission history, newest first
  POST /missions                    JSON {objective, node, mode?, scenario_id?, speed?} or multipart
                                    (same fields + files[]) -> Mission
  POST /missions/{id}/cancel        stop a running mission (live or simulated) -> Mission
  POST /missions/{id}/attachments   multipart files[] -> Mission
  GET  /missions/{id}/files/{attachments|outputs}/{name}   download a stored file
  POST /missions/{id}/messages      {text} mission thread (guidance, follow-up rounds) -> AgentMessage
  POST /approvals/{id}/decision     {decision: APPROVED|REJECTED, note?} -> ApprovalRequest
  POST /reset                       clear all missions (dev only)
  Inbox (docs/INBOX.md, routes/inbox.py): /inbox/status, /inbox/scan, /inbox/connect[/status], /followups,
                                    /followups/{id}[/draft], /drafts, /drafts/{id}/decision, /drafts/{id}/eml
  WS   /ws?since=<seq>              replay events with seq > since, then live

Run: `uv run python -m atlas` (required on Windows for the subscription backend, see atlas/__main__.py).
Contract: docs/EVENTS.md, docs/LIVE.md. Env: ATLAS_AGENTS_DIR, ATLAS_CORS_ORIGINS, ATLAS_SIM_SPEED
(default 1.0), plus the live-mode variables in .env.example. `.env` at the repo root is loaded at
startup without overriding variables already set (skipped under pytest).
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .core import paths
from .core.db import EventLog
from .core.events import EventBus
from .core.models import AgentDefinition
from .core.registry import DEFAULT_AGENTS_DIR, AgentRegistry
from .core.store import WorldStore
from .inbox.engine import InboxEngine
from .inbox.scheduler import InboxScheduler
from .inbox.sources import make_source
from .live.orchestrator import LiveEngine
from .routes import approvals, inbox, live, missions, stream, world
from .sim.runner import ScenarioLibrary, Simulator

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dotenv() -> None:
    # Tests must never pick up a real API key from a developer's .env (it would start live missions).
    if "pytest" in sys.modules:
        return
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)


_load_dotenv()
registry = AgentRegistry.load(os.getenv("ATLAS_AGENTS_DIR") or DEFAULT_AGENTS_DIR)


def _default_speed() -> float:
    try:
        speed = float(os.getenv("ATLAS_SIM_SPEED", "1.0"))
    except ValueError:
        return 1.0
    return speed if speed > 0 else 1.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = EventLog(paths.db_path())
    bus = EventBus(log=db)
    store = WorldStore(registry, bus)
    replayed = store.restore()  # mission history (docs/PHASE3.md B)
    library = ScenarioLibrary(registry)
    sim = Simulator(store, library, default_speed=_default_speed())
    live_engine = LiveEngine(store)
    app.state.registry = registry
    app.state.bus = bus
    app.state.store = store
    app.state.sim = sim
    app.state.live = live_engine
    inbox_engine = InboxEngine(store, live_engine, source_factory=make_source)  # docs/INBOX.md
    inbox_scheduler = InboxScheduler(inbox_engine)
    app.state.inbox = inbox_engine
    app.state.inbox_scheduler = inbox_scheduler
    n_scenarios = len(library.all())
    info = live_engine.backend_info()
    live_line = (
        f"live agents on {'your Claude plan (Claude Code)' if info.backend == 'subscription' else 'the Claude API'}"
        if info.available else f"live agents off ({info.hint})"
    )
    await store.log(
        f"ATLAS online with {len(registry.all())} agents · {n_scenarios} simulated scenarios · {live_line}.",
        agent_id=registry.orchestrator.id,
    )
    if replayed:
        interrupted = await store.recover_interrupted()
        n_missions = len(store.snapshot().missions)
        await store.log(
            f"Mission history restored · {n_missions} missions from {replayed} events"
            + (f" · {len(interrupted)} interrupted" if interrupted else ""),
            agent_id=registry.orchestrator.id,
        )
    await inbox_scheduler.start()
    try:
        yield
    finally:
        await inbox_scheduler.stop()  # fast: cancels the sleep; a running scan is left for recover_interrupted
        await inbox_engine.stop()
        # stop the engines without closing their missions: on the next start they come back interrupted
        await live_engine.stop_all()
        await sim.cancel_all()
        db.close()


app = FastAPI(
    title="ATLAS", version=__version__, description="One Intelligence. Many Agents.", lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ATLAS_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/agents", response_model=list[AgentDefinition])
def list_agents() -> list[AgentDefinition]:
    return registry.all()


app.include_router(world.router)
app.include_router(missions.router)
app.include_router(live.router)
app.include_router(approvals.router)
app.include_router(inbox.router)
app.include_router(stream.router)
