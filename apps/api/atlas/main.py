"""ATLAS API — Phase 0 shell.

Endpoints available now:
  GET  /health            liveness
  GET  /agents            agent registry (definitions)
  GET  /events            event history (?since=<seq>)
  WS   /ws                live event stream for the Command Center

Mission/task/approval endpoints arrive in Phases 1–3 (see docs/ROADMAP.md).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .core.events import EventBus
from .core.models import AgentDefinition, AtlasEvent, EventType
from .core.registry import DEFAULT_AGENTS_DIR, AgentRegistry

registry = AgentRegistry.load(os.getenv("ATLAS_AGENTS_DIR") or DEFAULT_AGENTS_DIR)
bus = EventBus()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await bus.publish(
        AtlasEvent(
            type=EventType.LOG,
            agent_id=registry.orchestrator.id,
            summary=f"ATLAS online with {len(registry.all())} agents.",
        )
    )
    yield


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


@app.get("/events", response_model=list[AtlasEvent])
def list_events(since: int = 0, mission_id: str | None = None) -> list[AtlasEvent]:
    return bus.history(since_seq=since, mission_id=mission_id)


@app.websocket("/ws")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    queue = bus.subscribe()
    try:
        for event in bus.history():
            await ws.send_text(event.model_dump_json())
        while True:
            event = await queue.get()
            await ws.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
