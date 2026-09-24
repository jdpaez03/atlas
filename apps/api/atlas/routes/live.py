"""GET /config · GET /agents/availability (Phase 2, docs/LIVE.md)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from .deps import LiveDep

router = APIRouter(tags=["live"])


class ModelsInfo(BaseModel):
    orchestrator: str
    default: str
    fast: str


class ConfigInfo(BaseModel):
    live_available: bool
    backend: Literal["api", "subscription"] | None  # what runs live missions (ATLAS_LLM_BACKEND)
    cost_basis: Literal["api", "api_equivalent"] | None  # api_equivalent: plan usage priced at API rates
    live_hint: str | None  # why live is unavailable (None when it is)
    models: ModelsInfo
    web_search: bool
    context_nodes: list[str]


class Availability(BaseModel):
    available: bool
    reason: str | None = None


@router.get("/config", response_model=ConfigInfo)
async def get_config(live: LiveDep) -> ConfigInfo:  # async: the Windows event-loop check needs the loop
    info = live.backend_info()
    cfg = live.config_for(info.backend or "api")
    m = cfg.models
    return ConfigInfo(
        live_available=info.available,
        backend=info.backend,  # type: ignore[arg-type]
        cost_basis=info.cost_basis,  # type: ignore[arg-type]
        live_hint=info.hint,
        models=ModelsInfo(orchestrator=m.orchestrator, default=m.default, fast=m.fast),
        web_search=cfg.web_search,
        context_nodes=live.context.nodes_with_context(),
    )


@router.get("/agents/availability", response_model=dict[str, Availability],
            response_model_exclude_none=True)
def agents_availability(live: LiveDep) -> dict[str, Any]:
    return live.loader.availability()
