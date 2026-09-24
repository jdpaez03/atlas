"""Read side + dev commands: GET /state, /nodes, /scenarios, /events · POST /reset."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.models import AtlasEvent, NodeDefinition, WorldState
from .deps import BusDep, LiveDep, RegistryDep, SimDep, StoreDep

router = APIRouter(tags=["world"])


class ScenarioInfo(BaseModel):
    id: str
    node: str
    title: str
    objective: str


@router.get("/state", response_model=WorldState)
def get_state(store: StoreDep) -> WorldState:
    return store.snapshot()


@router.get("/nodes", response_model=list[NodeDefinition])
def list_nodes(registry: RegistryDep) -> list[NodeDefinition]:
    return registry.nodes()


@router.get("/scenarios", response_model=list[ScenarioInfo])
def list_scenarios(sim: SimDep) -> list[ScenarioInfo]:
    return [
        ScenarioInfo(id=sc.id, node=sc.node, title=sc.title, objective=sc.objective)
        for sc in sim.library.all().values()
    ]


@router.get("/events", response_model=list[AtlasEvent])
def list_events(bus: BusDep, since: int = 0, mission_id: str | None = None) -> list[AtlasEvent]:
    return bus.history(since_seq=since, mission_id=mission_id)


@router.post("/reset")
async def reset(sim: SimDep, live: LiveDep) -> dict[str, str]:
    await live.cancel_all()
    await sim.reset()
    return {"status": "reset"}
