"""Mission history (SQLite event log), restart recovery, attachments and downloads (docs/PHASE3.md B)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core import paths
from atlas.core.db import EventLog
from atlas.core.events import EventBus
from atlas.core.models import AtlasEvent, Mission, WorldState
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live.llm import call_text, tool_use

MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")

PAUSE_SCENARIO = {
    "id": "history-pause",
    "node": "corporate",
    "title": "Pause",
    "objective": "Pause at an approval",
    "steps": [
        {"do": "phase", "phase": "DECOMPOSITION"},
        {"do": "task", "ref": "t1", "title": "Draft", "description": "d", "assigned_to": "alfred"},
        {"do": "task", "ref": "t2", "title": "Done", "description": "d", "assigned_to": "sofia"},
        {"do": "task_update", "ref": "t2", "status": "COMPLETED"},
        {"do": "agent", "agent": "alfred", "status": "WORKING", "task": "t1", "activity": "Drafting"},
        {"do": "approval", "ref": "a", "requested_by": "alfred", "reason": "EXTERNAL_COMMUNICATION",
         "title": "Send", "detail": "d", "task": "t1"},
        {"do": "phase", "phase": "CLOSED"},
    ],
}


def stage(name: str):
    return lambda kw: f"STAGE: {name}" in call_text(kw)


def task_call(title: str):
    return lambda kw: f"Your task: {title}" in call_text(kw)


def t(ref: str, agent: str, title: str, deps: list[str] | None = None, **kw) -> dict:
    return {"ref": ref, "title": title, "description": f"Do {title.lower()}", "assigned_to": agent,
            "depends_on": deps or [], **kw}


def report(statement: str):
    return tool_use("submit_report", {
        "asked_to": "the task", "actions_taken": ["worked"], "inputs_used": ["context"],
        "findings": [{"kind": "FACT", "statement": statement, "sources": ["ctx"], "confidence": "HIGH"}],
        "confidence": "HIGH",
    })


async def until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.001)


def _poll(client: TestClient, pred, timeout: float = 3.0) -> WorldState:
    deadline = time.monotonic() + timeout
    while True:
        state = WorldState.model_validate(client.get("/state").json())
        if pred(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def _same(a: WorldState, b: WorldState) -> bool:
    def norm(s: WorldState) -> dict:
        d = s.model_dump(mode="json")
        for st in d["agent_states"]:
            st.pop("updated_at")
        return d

    return norm(a) == norm(b)


def make_live(store: WorldStore, llm: FakeLLM) -> LiveEngine:
    return LiveEngine(
        store, loader=AgentLoader(store.registry, MODELS),
        context=NodeContext(store.registry, local_dir=Path("/nonexistent-atlas-local")),
        config=LiveConfig(models=MODELS, max_turns=5), llm=llm, prices=PriceTable(),
    )


def paused_llm() -> FakeLLM:
    llm = FakeLLM()
    llm.when(stage("PLANNING"), tool_use("create_plan", {"tasks": [
        t("research", "sofia", "Market research"),
        t("send", "alfred", "Send offer", ["research"]),
    ]}))
    llm.when(task_call("Market research"), report("Rent is 18k"))
    llm.when(task_call("Send offer"), tool_use("request_approval", {
        "reason": "EXTERNAL_COMMUNICATION", "title": "Send the offer", "detail": "to the seller"}))
    return llm


# ---------------------------------------------------------------------------
# event log + restart
# ---------------------------------------------------------------------------


def test_event_log_roundtrip(tmp_path: Path):
    db = EventLog(tmp_path / "x" / "atlas.db")
    e = AtlasEvent(seq=3, type="log", summary="hello", mission_id="m1")
    db.append(e)
    db.append(AtlasEvent(seq=4, type="log", summary="other"))
    assert db.last_seq() == 4 and db.count() == 2
    assert [x.summary for x in db.events(0, "m1")] == ["hello"]
    assert [x.seq for x in db.events(3)] == [4]
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.clear()
    assert db.count() == 0
    db.close()


def test_restart_restores_state_and_marks_interrupted(registry, tmp_path: Path):
    path = tmp_path / "atlas.db"

    async def first_run():
        store = WorldStore(registry, EventBus(log=EventLog(path)))
        engine = make_live(store, paused_llm())
        m = await engine.start("Buy the Polanco 2BR", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        snap = store.snapshot()
        await engine.stop_all()  # like a shutdown: nothing closes the mission
        return m.id, snap, store.snapshot()

    mid, paused, stopped = asyncio.run(first_run())
    assert next(x for x in stopped.missions if x.id == mid).phase != "CLOSED"

    async def second_run():
        bus = EventBus(log=EventLog(path))
        store = WorldStore(registry, bus)
        n = store.restore()
        restored = store.snapshot()
        interrupted = await store.recover_interrupted()
        return n, restored, interrupted, store

    n, restored, interrupted, store = asyncio.run(second_run())
    assert n == stopped.last_seq
    # the replayed state is exactly the state at shutdown (agents never touched keep a fresh updated_at)
    assert _same(restored, stopped)
    assert paused.missions[0].id == mid and restored.last_seq == stopped.last_seq

    # interrupted handling, as normal events
    assert interrupted == [mid]
    snap = store.snapshot()
    mission = snap.missions[0]
    assert mission.interrupted and mission.phase == "CLOSED" and mission.closed_at
    by_title = {x.title: x.status for x in snap.tasks}
    assert by_title == {"Market research": "COMPLETED", "Send offer": "CANCELLED"}
    assert snap.approvals[0].state == "EXPIRED"
    assert all(s.status in ("IDLE", "MONITORING") and s.current_task_id is None for s in snap.agent_states)
    assert any(e.type == "log" and "interrupted" in e.summary for e in store.bus.history(stopped.last_seq))
    # new events continue the sequence, and the whole log folds into the snapshot
    assert store.bus.history(stopped.last_seq)[0].seq == stopped.last_seq + 1
    base = WorldStore(registry, EventBus()).snapshot()
    assert _same(fold(base, store.bus.history()), snap)


def test_http_restart_ws_replay_history_and_reset():
    from atlas.main import app
    from atlas.sim.scenario import Scenario

    with TestClient(app) as client:
        app.state.sim.library.directory = None
        app.state.sim.library.add(Scenario.model_validate(PAUSE_SCENARIO))
        r = client.post("/missions", json={"objective": "Sim A", "node": "corporate",
                                           "scenario_id": "history-pause", "speed": 1000})
        first = r.json()["id"]
        _poll(client, lambda s: any(a.state == "PENDING" for a in s.approvals))
        r = client.post("/missions", json={"objective": "Sim B", "node": "corporate",
                                           "scenario_id": "history-pause", "speed": 1000})
        second = r.json()["id"]
        _poll(client, lambda s: sum(a.state == "PENDING" for a in s.approvals) == 2)
        client.post(f"/missions/{first}/cancel")
        before = client.get("/state").json()
        events_before = client.get("/events").json()
    assert paths.db_path().is_file()

    with TestClient(app) as client:  # "restart": same ATLAS_LOCAL_DIR, new store and bus
        state = WorldState.model_validate(client.get("/state").json())
        ms = {m.id: m for m in state.missions}
        assert ms[first].phase == "CLOSED" and not ms[first].interrupted  # was cancelled, not interrupted
        assert ms[second].phase == "CLOSED" and ms[second].interrupted
        assert {a.state for a in state.approvals} == {"EXPIRED"}
        assert state.last_seq > before["last_seq"]

        # WS replay from 0 returns the persisted events first, in order
        with client.websocket_connect("/ws?since=0") as ws:
            frames = [ws.receive_json() for _ in range(len(events_before))]
        assert [f["seq"] for f in frames] == [e["seq"] for e in events_before]
        assert frames[0]["summary"].startswith("ATLAS online")
        with client.websocket_connect(f"/ws?since={before['last_seq']}") as ws:
            nxt = ws.receive_json()
        assert nxt["seq"] == before["last_seq"] + 1

        # history API: newest first, filters
        hist = client.get("/missions").json()
        assert [h["id"] for h in hist] == [second, first]
        assert set(hist[0]) == {"id", "objective", "node", "mode", "phase", "round", "interrupted",
                                "created_at", "closed_at", "report_versions", "usage"}
        assert hist[0]["interrupted"] is True and hist[0]["round"] == 1 and hist[0]["report_versions"] == []
        assert [h["id"] for h in client.get("/missions?limit=1").json()] == [second]
        assert client.get("/missions?node=personal").json() == []

        assert client.post("/reset").status_code == 200
        db = EventLog(paths.db_path())
        assert db.count() == 1 and next(db.events()).payload.get("reset") is True
        db.close()

    with TestClient(app) as client:  # after a reset nothing comes back
        assert client.get("/state").json()["missions"] == []
        assert client.get("/missions").json() == []


# ---------------------------------------------------------------------------
# attachments + downloads
# ---------------------------------------------------------------------------


@pytest.fixture
def sim_client():
    from atlas.main import app
    from atlas.sim.scenario import Scenario

    with TestClient(app) as client:
        app.state.sim.library.directory = None
        app.state.sim.library.add(Scenario.model_validate(PAUSE_SCENARIO))
        yield client


def _create(client: TestClient, files: list[tuple[str, bytes]], **fields):
    data = {"objective": "With files", "node": "corporate", "mode": "simulated", "scenario_id": "history-pause",
            "speed": "1000", **fields}
    return client.post("/missions", data=data, files=[("files", (n, c, "application/octet-stream"))
                                                      for n, c in files])


def test_multipart_create_with_files_and_download(sim_client: TestClient):
    c = sim_client
    r = _create(c, [("Rocks_Q3.xlsx", b"xlsx-bytes"), ("../../etc/pass wd?.txt", b"hello")])
    assert r.status_code == 200, r.text
    m = Mission.model_validate(r.json())
    assert m.mode == "simulated" and m.objective == "With files"
    names = [a.name for a in m.attachments]
    assert names == ["Rocks_Q3.xlsx", "pass wd_.txt"]  # safe names, no path parts
    a = m.attachments[0]
    assert a.kind == "file" and a.size_bytes == 10
    assert a.download_url == f"/missions/{m.id}/files/attachments/Rocks_Q3.xlsx"
    folder = paths.attachments_dir(m.id)
    assert (folder / "Rocks_Q3.xlsx").read_bytes() == b"xlsx-bytes"
    assert sorted(p.name for p in folder.iterdir()) == ["Rocks_Q3.xlsx", "pass wd_.txt"]

    r = c.get(a.download_url)
    assert r.status_code == 200 and r.content == b"xlsx-bytes"
    assert c.get(m.attachments[1].download_url).content == b"hello"

    # more files later, same name -> "name (2).ext"; mission.updated carries the full mission
    r = c.post(f"/missions/{m.id}/attachments", files=[("files[]", ("Rocks_Q3.xlsx", b"v2", "text/plain"))])
    assert r.status_code == 200
    assert [x["name"] for x in r.json()["attachments"]][-1] == "Rocks_Q3 (2).xlsx"
    # regression: names with spaces/parentheses must download (every renamed duplicate has them)
    dup = r.json()["attachments"][-1]
    r2 = c.get(dup["download_url"].replace(" ", "%20"))
    assert r2.status_code == 200 and r2.content == b"v2"
    ev = [e for e in c.get(f"/events?mission_id={m.id}").json() if e["type"] == "mission.updated"][-1]
    assert len(ev["payload"]["mission"]["attachments"]) == 3
    state = WorldState.model_validate(c.get("/state").json())
    assert len(next(x for x in state.missions if x.id == m.id).attachments) == 3

    # JSON still works
    r = c.post("/missions", json={"objective": "json", "node": "corporate", "scenario_id": "history-pause",
                                  "speed": 1000})
    assert r.status_code == 200 and r.json()["attachments"] == []
    assert c.post("/missions", json={"node": "corporate"}).status_code == 422
    assert _create(c, [], objective="").status_code == 422


def test_upload_limits(sim_client: TestClient, monkeypatch):
    from atlas.routes import missions as routes

    c = sim_client
    monkeypatch.setattr(routes, "MAX_FILE_BYTES", 8)
    r = _create(c, [("ok.txt", b"12345678"), ("big.txt", b"123456789")])
    assert r.status_code == 413
    assert c.get("/state").json()["missions"] == []  # nothing created, nothing left on disk
    assert not (paths.local_dir() / "missions").exists() or not any((paths.local_dir() / "missions").iterdir())

    monkeypatch.setattr(routes, "MAX_FILE_BYTES", 1024)
    monkeypatch.setattr(routes, "MAX_FILES_PER_MISSION", 2)
    assert _create(c, [("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c")]).status_code == 422
    r = _create(c, [("a.txt", b"a"), ("b.txt", b"b")])
    assert r.status_code == 200
    mid = r.json()["id"]
    r = c.post(f"/missions/{mid}/attachments", files=[("files", ("c.txt", b"c", "text/plain"))])
    assert r.status_code == 422 and "at most 2" in r.json()["detail"]
    assert c.post(f"/missions/{mid}/attachments", files=[("files", ("x", b"", "text/plain"))]).status_code == 422
    assert c.post("/missions/msn_nope/attachments", files=[("files", ("a", b"a", "text/plain"))]).status_code == 404


def test_download_path_safety(sim_client: TestClient, tmp_path: Path):
    c = sim_client
    r = _create(c, [("a.txt", b"a")])
    mid = r.json()["id"]
    m = Mission.model_validate(r.json())
    secret = paths.db_path()
    assert secret.is_file()

    for bad in ("..%2F..%2F..%2Fatlas.db", "%2E%2E", "..", ".env", "a.txt%00", "sub%2Fa.txt"):
        r = c.get(f"/missions/{mid}/files/attachments/{bad}")
        assert r.status_code in (400, 404), (bad, r.status_code)
        assert r.content != secret.read_bytes()
    assert c.get(f"/missions/{mid}/files/secrets/a.txt").status_code == 422  # unknown kind
    assert c.get("/missions/msn_nope/files/attachments/a.txt").status_code == 404

    # outputs resolve inside outputs/<node>/<mission>; a symlink escaping it is refused
    out = paths.outputs_dir(m.node, mid)
    out.mkdir(parents=True)
    (out / "memo.md").write_text("# memo")
    r = c.get(f"/missions/{mid}/files/outputs/memo.md")
    assert r.status_code == 200 and r.text == "# memo"
    assert "attachment" in r.headers["content-disposition"]
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    try:
        os.symlink(outside, out / "link.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert c.get(f"/missions/{mid}/files/outputs/link.txt").status_code == 404
    # another mission's outputs are not reachable through this mission
    assert c.get(f"/missions/{mid}/files/attachments/memo.md").status_code == 404
