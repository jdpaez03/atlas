"""Accessors for the engine objects the app creates in its lifespan (see atlas/main.py)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket

from ..core.events import EventBus
from ..core.registry import AgentRegistry
from ..core.store import ConflictError, IsolationError, NotFoundError, StoreError, WorldStore
from ..live.orchestrator import LiveEngine
from ..sim.runner import Simulator


def _app(conn: Request | WebSocket):
    return conn.app


def get_store(request: Request) -> WorldStore:
    return _app(request).state.store


def get_bus(request: Request) -> EventBus:
    return _app(request).state.bus


def get_registry(request: Request) -> AgentRegistry:
    return _app(request).state.registry


def get_sim(request: Request) -> Simulator:
    return _app(request).state.sim


def get_live(request: Request) -> LiveEngine:
    return _app(request).state.live


StoreDep = Annotated[WorldStore, Depends(get_store)]
BusDep = Annotated[EventBus, Depends(get_bus)]
RegistryDep = Annotated[AgentRegistry, Depends(get_registry)]
SimDep = Annotated[Simulator, Depends(get_sim)]
LiveDep = Annotated[LiveEngine, Depends(get_live)]


def http_error(exc: Exception) -> HTTPException:
    """Map store errors to HTTP status codes."""
    if isinstance(exc, NotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (IsolationError, StoreError, ValueError)):
        return HTTPException(422, str(exc))
    raise exc
