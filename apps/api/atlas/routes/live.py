"""GET /config · GET /agents/availability (Phase 2, docs/LIVE.md)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .deps import LiveDep
from .missions import live_available

router = APIRouter(tags=["live"])


class ModelsInfo(BaseModel):
    orchestrator: str
    default: str
    fast: str


class ConfigInfo(BaseModel):
    live_available: bool
    models: ModelsInfo
    web_search: bool
    context_nodes: list[str]


class Availability(BaseModel):
    available: bool
    reason: str | None = None


@router.get("/config", response_model=ConfigInfo)
def get_config(live: LiveDep) -> ConfigInfo:
    m = live.config.models
    return ConfigInfo(
        live_available=live_available(),
        models=ModelsInfo(orchestrator=m.orchestrator, default=m.default, fast=m.fast),
        web_search=live.config.web_search,
        context_nodes=live.context.nodes_with_context(),
    )


@router.get("/agents/availability", response_model=dict[str, Availability],
            response_model_exclude_none=True)
def agents_availability(live: LiveDep) -> dict[str, Any]:
    return live.loader.availability()
