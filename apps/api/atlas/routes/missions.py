"""POST /missions — Phase 1: every mission is played by the simulator."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.models import Mission
from .deps import RegistryDep, SimDep, http_error

router = APIRouter(tags=["missions"])


class MissionCreate(BaseModel):
    objective: str = Field(min_length=1)
    node: str
    scenario_id: str | None = None
    speed: float | None = Field(default=None, gt=0)


@router.post("/missions", response_model=Mission)
async def create_mission(
    body: MissionCreate,
    sim: SimDep,
    registry: RegistryDep,
) -> Mission:
    enabled = {n.id for n in registry.nodes()}
    if body.node not in enabled:
        try:
            registry.agents_for_node(body.node)
        except ValueError:
            raise HTTPException(404, f"unknown node '{body.node}'") from None
        raise HTTPException(422, f"node '{body.node}' is disabled")

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
            raise HTTPException(
                422, f"no simulated scenario available for node '{body.node}' (Phase 1 runs scenarios only)"
            )
    try:
        return await sim.start(scenario, objective=body.objective, node=body.node, speed=body.speed)
    except ValueError as exc:
        raise http_error(exc) from exc
