"""POST /missions (simulated or live) · POST /missions/{id}/cancel.

Mode resolution (docs/LIVE.md):
  mode "live"       -> live orchestrator on the configured backend (ATLAS_LLM_BACKEND: api | subscription);
                       422 with the reason when no backend is usable
  mode "simulated"  -> scenario (scenario_id, or the node's first scenario)
  no mode           -> simulated if a scenario_id is given or live is unavailable, else live
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.models import Mission
from ..core.store import StoreError
from .deps import LiveDep, RegistryDep, SimDep, StoreDep, http_error

router = APIRouter(tags=["missions"])


class MissionCreate(BaseModel):
    objective: str = Field(min_length=1)
    node: str
    mode: Literal["simulated", "live"] | None = None
    scenario_id: str | None = None
    speed: float | None = Field(default=None, gt=0)


def resolve_mode(body: MissionCreate, live_available: bool) -> str:
    if body.mode:
        return body.mode
    if body.scenario_id or not live_available:
        return "simulated"
    return "live"


@router.post("/missions", response_model=Mission)
async def create_mission(
    body: MissionCreate,
    sim: SimDep,
    live: LiveDep,
    registry: RegistryDep,
) -> Mission:
    enabled = {n.id for n in registry.nodes()}
    if body.node not in enabled:
        try:
            registry.agents_for_node(body.node)
        except ValueError:
            raise HTTPException(404, f"unknown node '{body.node}'") from None
        raise HTTPException(422, f"node '{body.node}' is disabled")

    info = live.backend_info()
    if resolve_mode(body, info.available) == "live":
        if not info.available:
            raise HTTPException(422, f"live mode is unavailable: {info.hint}")
        if body.scenario_id:
            raise HTTPException(422, "scenario_id is only valid for simulated missions")
        try:
            return await live.start(body.objective, body.node, backend=info.backend)
        except StoreError as exc:
            raise http_error(exc) from exc

    if body.scenario_id:
        scenario = sim.library.get(body.scenario_id)
        if scenario is None:
            raise HTTPException(404, f"unknown scenario '{body.scenario_id}'")
        if scenario.node != body.node:
            raise HTTPException(
                422, f"scenario '{scenario.id}' belongs to node '{scenario.node}', not '{body.node}'"
            )
    else:
        scenario = sim.library.default_for(body.node)
        if scenario is None:
            raise HTTPException(422, f"no simulated scenario available for node '{body.node}'")
    try:
        return await sim.start(scenario, objective=body.objective, node=body.node, speed=body.speed)
    except ValueError as exc:
        raise http_error(exc) from exc


@router.post("/missions/{mission_id}/cancel", response_model=Mission)
async def cancel_mission(mission_id: str, sim: SimDep, live: LiveDep, store: StoreDep) -> Mission:
    try:
        store.mission(mission_id)  # 404 for unknown missions
        if not (await sim.cancel(mission_id) or await live.cancel(mission_id)):
            await store.cancel_mission(mission_id)  # nothing running it: just close it (409 if closed)
        return store.mission(mission_id)
    except StoreError as exc:
        raise http_error(exc) from exc
