"""Engine tests: WorldStore, reducer, simulator, HTTP + WS routes.

Uses an inline scenario (not the files in atlas/sim/scenarios) played at high speed.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core.events import EventBus
from atlas.core.models import (
    AgentMessage,
    AgentReport,
    AgentState,
    ApprovalRequest,
    EventType,
    Mission,
    MissionReport,
    Task,
    WorldState,
)
from atlas.core.registry import DEFAULT_AGENTS_DIR, AgentRegistry
from atlas.core.store import IsolationError, StoreError, WorldStore, fold
from atlas.sim.runner import ScenarioLibrary, Simulator
from atlas.sim.scenario import Scenario

SPEED = 2000.0

SCENARIO = {
    "id": "test-inline",
    "node": "corporate",
    "title": "Inline test mission",
    "objective": "Test the engine end to end.",
    "steps": [
        {"do": "phase", "phase": "DECOMPOSITION"},
        {"do": "task", "ref": "research", "title": "Market research", "description": "comps",
         "assigned_to": "sofia", "priority": "HIGH"},
        {"do": "task", "ref": "analysis", "title": "Scenario analysis", "description": "irr",
         "assigned_to": "oracle", "depends_on": ["research"]},
        {"do": "task", "ref": "package", "title": "Broker package", "description": "send",
         "assigned_to": "alfred", "depends_on": ["analysis"], "requires_approval": True,
         "approval_reason": "EXTERNAL_COMMUNICATION"},
        {"do": "phase", "phase": "EXECUTION"},
        {"do": "agent", "agent": "argos", "status": "WORKING", "activity": "Watching listings"},
        {"do": "agent", "agent": "sofia", "status": "WORKING", "activity": "Pulling comps", "task": "research"},
        {"do": "task_update", "ref": "research", "status": "IN_PROGRESS", "progress": 0.4},
        {"do": "message", "from": "oracle", "to": "sofia", "type": "REQUEST",
         "subject": "Rental comps for 2BR units", "body": "Need comps", "task": "analysis",
         "requires_response": True},
        {"do": "agent", "agent": "oracle", "status": "COLLABORATING", "task": "analysis",
         "with": ["sofia"], "activity": "Aligning on comps"},
        {"do": "report", "agent": "sofia", "task": "research", "asked_to": "Find comps",
         "findings": [{"kind": "FACT", "statement": "Avg rent 18k", "confidence": "HIGH"}],
         "confidence": "HIGH"},
        {"do": "task_update", "ref": "research", "status": "COMPLETED"},
        {"do": "task_update", "ref": "analysis", "status": "COMPLETED"},
        {"do": "approval", "ref": "send", "requested_by": "alfred", "reason": "EXTERNAL_COMMUNICATION",
         "title": "Send package to broker", "detail": "Email the package", "task": "package",
         "on_reject": [{"do": "log", "agent": "atlas", "text": "Package held for revision."},
                       {"do": "task_update", "ref": "package", "status": "CANCELLED"}]},
        {"do": "phase", "phase": "REPORTING"},
        {"do": "mission_report", "executive_summary": "Looks fine.", "objective_status": "PARTIAL",
         "key_findings": [{"kind": "RECOMMENDATION", "statement": "Proceed"}]},
        {"do": "phase", "phase": "CLOSED"},
    ],
}

PAYLOAD_KEY = {
    EventType.MISSION_CREATED: ("mission", Mission),
    EventType.MISSION_PHASE_CHANGED: ("mission", Mission),
    EventType.MISSION_CLOSED: ("mission", Mission),
    EventType.TASK_CREATED: ("task", Task),
    EventType.TASK_UPDATED: ("task", Task),
    EventType.AGENT_STATE_CHANGED: ("state", AgentState),
    EventType.MESSAGE_SENT: ("message", AgentMessage),
    EventType.REPORT_SUBMITTED: ("report", AgentReport),
    EventType.MISSION_REPORT_READY: ("report", MissionReport),
    EventType.APPROVAL_REQUESTED: ("approval", ApprovalRequest),
    EventType.APPROVAL_DECIDED: ("approval", ApprovalRequest),
}


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def scenario() -> Scenario:
    return Scenario.model_validate(SCENARIO)


def make_engine(registry: AgentRegistry) -> tuple[WorldStore, Simulator]:
    store = WorldStore(registry, EventBus())
    library = ScenarioLibrary(registry, directory=None)
    library.add(scenario())
    return store, Simulator(store, library, default_speed=SPEED)


async def _until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.001)


async def _run(registry: AgentRegistry, decision: str) -> tuple[WorldStore, Simulator, WorldState, str]:
    store, sim = make_engine(registry)
    initial = store.snapshot()
    mission = await sim.start(scenario(), objective="Evaluate the Polanco 2BR")
    await _until(lambda: (r := sim.runner(mission.id)) is not None and r.waiting_on is not None)
    approval_id = sim.runner(mission.id).waiting_on

    # paused on approval
    snap = store.snapshot()
    pkg = next(t for t in snap.tasks if t.title == "Broker package")
    alfred = next(s for s in snap.agent_states if s.agent_id == "alfred")
    assert pkg.status == "AWAITING_APPROVAL"
    assert alfred.status == "WAITING" and alfred.activity == "Awaiting human approval"
    assert next(m for m in snap.missions if m.id == mission.id).phase == "EXECUTION"
    await asyncio.sleep(0.02)
    assert sim.runner(mission.id).waiting_on == approval_id  # still paused

    await store.decide_approval(approval_id, decision, note="ok")
    await asyncio.wait_for(sim.wait(mission.id), 3)
    return store, sim, initial, mission.id


# ---------------------------------------------------------------------------


def test_approve_resumes_and_closes(registry):
    store, _, _, mid = asyncio.run(_run(registry, "APPROVED"))
    snap = store.snapshot()
    mission = next(m for m in snap.missions if m.id == mid)
    assert mission.phase == "CLOSED" and mission.closed_at is not None
    assert len(mission.task_ids) == 3
    report = next(r for r in snap.mission_reports if r.mission_id == mid)
    assert mission.final_report_id == report.id
    assert report.tasks_completed == ["Market research", "Scenario analysis"]
    assert report.tasks_pending == ["Broker package"]
    assert report.agent_report_ids == [snap.agent_reports[0].id]
    pkg = next(t for t in snap.tasks if t.title == "Broker package")
    assert pkg.status == "IN_PROGRESS"
    assert snap.approvals[0].state == "APPROVED" and snap.approvals[0].decision_note == "ok"
    # every touched agent released; ARGOS back to MONITORING
    states = {s.agent_id: s for s in snap.agent_states}
    assert states["argos"].status == "MONITORING"
    for a in ("sofia", "oracle", "alfred"):
        assert states[a].status == "IDLE" and states[a].current_task_id is None
    assert not any("held" in e.summary for e in store.bus.history())


def test_reject_plays_on_reject(registry):
    store, _, _, mid = asyncio.run(_run(registry, "REJECTED"))
    snap = store.snapshot()
    summaries = [e.summary for e in store.bus.history()]
    assert "Package held for revision." in summaries
    pkg = next(t for t in snap.tasks if t.title == "Broker package")
    assert pkg.status == "CANCELLED" and pkg.completed_at is not None
    assert next(m for m in snap.missions if m.id == mid).phase == "CLOSED"


def test_reducer_fold_reproduces_snapshot(registry):
    store, _, initial, _ = asyncio.run(_run(registry, "APPROVED"))
    folded = fold(initial, store.bus.history())
    assert folded.model_dump(mode="json") == store.snapshot().model_dump(mode="json")
    assert folded.last_seq == store.bus.last_seq


def test_reducer_handles_reset(registry):
    async def go():
        store, sim = make_engine(registry)
        initial = store.snapshot()
        m = await sim.start(scenario())
        await _until(lambda: sim.runner(m.id) and sim.runner(m.id).waiting_on)
        await sim.reset()
        return store, initial

    store, initial = asyncio.run(go())
    snap = store.snapshot()
    assert snap.missions == [] and snap.tasks == [] and snap.approvals == []
    assert all(s.status in ("IDLE", "MONITORING") for s in snap.agent_states)
    folded = fold(initial, store.bus.history())
    assert folded.model_dump(mode="json") == snap.model_dump(mode="json")


def test_event_payload_keys_and_summaries(registry):
    store, *_ = asyncio.run(_run(registry, "APPROVED"))
    events = store.bus.history()
    seen = set()
    for e in events:
        assert e.summary
        etype = EventType(e.type)
        seen.add(etype)
        if etype == EventType.LOG:
            continue
        key, model = PAYLOAD_KEY[etype]
        obj = model.model_validate(e.payload[key])
        expected_mid = obj.id if isinstance(obj, Mission) else getattr(obj, "mission_id", e.mission_id)
        assert e.mission_id == expected_mid
    assert seen >= set(PAYLOAD_KEY)
    first = events[0]
    assert first.type == "mission.created" and first.payload["mission"]["phase"] == "OBJECTIVE"
    msgs = [e for e in events if e.type == "message.sent"]
    assert msgs[0].summary == "ORACLE → SOFIA · REQUEST · Rental comps for 2BR units"
    assert msgs[0].payload["message"]["from"] == "oracle"  # wire format uses aliases
    created = [e.summary for e in events if e.type == "task.created"]
    assert created[0] == "ATLAS assigned 'Market research' to SOFIA"
    # payloads are snapshots, not live references
    research = [e for e in events if e.type == "task.updated" and e.payload["task"]["title"] == "Market research"]
    assert research[0].payload["task"]["status"] == "IN_PROGRESS"


def test_dependency_promotion(registry):
    async def go():
        store = WorldStore(registry, EventBus())
        m = await store.create_mission("x", "corporate")
        a = await store.create_task(m.id, "A", "a", "sofia")
        b = await store.create_task(m.id, "B", "b", "oracle", depends_on=[a.id])
        c = await store.create_task(m.id, "C", "c", "alfred", depends_on=[a.id, b.id])
        assert (a.status, b.status, c.status) == ("READY", "PENDING", "PENDING")
        a = await store.update_task(a.id, status="IN_PROGRESS")
        assert a.started_at is not None and a.completed_at is None
        await store.update_task(a.id, status="COMPLETED")
        assert store.task(a.id).completed_at is not None and store.task(a.id).progress == 1.0
        assert store.task(b.id).status == "READY"
        assert store.task(c.id).status == "PENDING"
        await store.update_task(b.id, status="COMPLETED")
        assert store.task(c.id).status == "READY"
        assert store.task(b.id).started_at is not None

    asyncio.run(go())


def test_node_isolation_errors(tmp_path: Path):
    shutil.copytree(DEFAULT_AGENTS_DIR, tmp_path / "agents")
    org = tmp_path / "agents" / "organization.yaml"
    org.write_text(org.read_text().replace("enabled: false", "enabled: true"))
    reg = AgentRegistry.load(tmp_path / "agents")

    async def go():
        store = WorldStore(reg, EventBus())
        m = await store.create_mission("Plan a trip", "personal")
        with pytest.raises(IsolationError):
            await store.create_task(m.id, "KPIs", "x", "eos-traction")
        t = await store.create_task(m.id, "Research", "x", "sofia")  # shared agent is fine
        with pytest.raises(IsolationError):
            await store.set_agent_state("eos-data", "WORKING", mission_id=m.id)
        with pytest.raises(IsolationError):
            await store.set_agent_state("sofia", "COLLABORATING", task_id=t.id, collaborating_with=["eos-data"])
        with pytest.raises(IsolationError):
            await store.send_message(m.id, "sofia", "eos-vision", "REQUEST", "s", "b")

    asyncio.run(go())

    async def disabled():
        store = WorldStore(AgentRegistry.load(), EventBus())
        with pytest.raises(StoreError, match="disabled"):
            await store.create_mission("x", "personal")

    asyncio.run(disabled())


# ---------------------------------------------------------------------------
# HTTP + WS
# ---------------------------------------------------------------------------


def _poll(client: TestClient, pred, timeout: float = 3.0) -> WorldState:
    deadline = time.monotonic() + timeout
    while True:
        state = WorldState.model_validate(client.get("/state").json())
        if pred(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_http_mission_flow_and_ws_replay():
    from atlas.main import app

    with TestClient(app) as client:
        sim = app.state.sim
        sim.library.directory = None  # only the inline scenario
        sim.library.add(scenario())

        assert [n["id"] for n in client.get("/nodes").json()] == ["corporate"]
        assert client.get("/scenarios").json() == [
            {"id": "test-inline", "node": "corporate", "title": "Inline test mission",
             "objective": "Test the engine end to end."}
        ]
        # errors
        r = client.post("/missions", json={"objective": "x", "node": "mars"})
        assert r.status_code == 404 and "unknown node" in r.json()["detail"]
        r = client.post("/missions", json={"objective": "x", "node": "personal"})
        assert r.status_code == 422 and "disabled" in r.json()["detail"]
        r = client.post("/missions", json={"objective": "x", "node": "corporate", "scenario_id": "nope"})
        assert r.status_code == 404 and "unknown scenario" in r.json()["detail"]

        # default scenario for the node
        r = client.post("/missions", json={"objective": "Evaluate deal", "node": "corporate", "speed": SPEED})
        assert r.status_code == 200, r.text
        mission = Mission.model_validate(r.json())
        assert mission.objective == "Evaluate deal" and mission.phase == "OBJECTIVE"

        state = _poll(client, lambda s: any(a.state == "PENDING" for a in s.approvals))
        apr = state.approvals[0]
        r = client.post(f"/approvals/{apr.id}/decision", json={"decision": "APPROVED", "note": "go"})
        assert r.status_code == 200 and r.json()["state"] == "APPROVED"
        r = client.post(f"/approvals/{apr.id}/decision", json={"decision": "REJECTED"})
        assert r.status_code == 409
        assert client.post("/approvals/apr_nope/decision", json={"decision": "APPROVED"}).status_code == 404

        state = _poll(client, lambda s: s.missions[0].phase == "CLOSED" and not sim.running())
        assert state.last_seq == client.get("/events").json()[-1]["seq"]
        assert client.get("/events", params={"mission_id": mission.id}).json()[0]["type"] == "mission.created"

        # WS replay from a given seq
        since = state.last_seq - 3
        with client.websocket_connect(f"/ws?since={since}") as ws:
            seqs = [ws.receive_json()["seq"] for _ in range(3)]
        assert seqs == [since + 1, since + 2, since + 3]

        # reset
        assert client.post("/reset").status_code == 200
        state = WorldState.model_validate(client.get("/state").json())
        assert state.missions == [] and state.tasks == []
        last = client.get("/events").json()[-1]
        assert last["type"] == "log" and last["payload"]["reset"] is True
