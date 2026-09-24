"""ATLAS API.

  GET  /health                      liveness
  GET  /agents                      agent registry (definitions)
  GET  /nodes                       enabled nodes
  GET  /scenarios                   simulated missions [{id, node, title, objective}]
  GET  /state                       WorldState snapshot (with last_seq)
  GET  /events?since=&mission_id=   event history
  POST /missions                    {objective, node, scenario_id?, speed?} -> Mission
  POST /approvals/{id}/decision     {decision: APPROVED|REJECTED, note?} -> ApprovalRequest
  POST /reset                       clear all missions (dev only)
  WS   /ws?since=<seq>              replay events with seq > since, then live

Contract: docs/EVENTS.md. Env: ATLAS_AGENTS_DIR, ATLAS_CORS_ORIGINS, ATLAS_SIM_SPEED (default 1.0).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .core.events import EventBus
from .core.models import AgentDefinition
from .core.registry import DEFAULT_AGENTS_DIR, AgentRegistry
from .core.store import WorldStore
from .routes import approvals, missions, stream, world
from .sim.runner import ScenarioLibrary, Simulator

registry = AgentRegistry.load(os.getenv("ATLAS_AGENTS_DIR") or DEFAULT_AGENTS_DIR)


def _default_speed() -> float:
    try:
        speed = float(os.getenv("ATLAS_SIM_SPEED", "1.0"))
    except ValueError:
        return 1.0
    return speed if speed > 0 else 1.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = EventBus()
    store = WorldStore(registry, bus)
    library = ScenarioLibrary(registry)
    sim = Simulator(store, library, default_speed=_default_speed())
    app.state.registry = registry
    app.state.bus = bus
    app.state.store = store
    app.state.sim = sim
    n_scenarios = len(library.all())
    await store.log(
        f"ATLAS online with {len(registry.all())} agents · {n_scenarios} simulated scenarios.",
        agent_id=registry.orchestrator.id,
    )
    try:
        yield
    finally:
        await sim.cancel_all()


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
app.include_router(approvals.router)
app.include_router(stream.router)
