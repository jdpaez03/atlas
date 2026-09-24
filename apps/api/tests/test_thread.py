"""Mission thread: human guidance while running, follow-up rounds after close, interrupted resume
(docs/PHASE3.md B). FakeLLM (api backend) and FakeClaudeSDK (subscription backend), no network."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core.db import EventLog
from atlas.core.events import EventBus
from atlas.core.models import HUMAN, WorldState
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live.llm import call_text, tool_use
from atlas.live.sdk import FakeClaudeSDK, call, say
from atlas.sim.runner import SIMULATED_REPLY

MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")


def stage(name: str):
    return lambda kw: f"STAGE: {name}" in call_text(kw)


def task_call(title: str):
    return lambda kw: f"Your task: {title}" in call_text(kw)


def t(ref: str, agent: str, title: str, deps: list[str] | None = None, **kw) -> dict:
    return {"ref": ref, "title": title, "description": f"Do {title.lower()}", "assigned_to": agent,
            "depends_on": deps or [], **kw}


REPORT_ARGS = {"asked_to": "the task", "actions_taken": ["worked"], "inputs_used": ["context"],
               "confidence": "HIGH"}


def findings(statement: str) -> list[dict]:
    return [{"kind": "FACT", "statement": statement, "sources": ["ctx"], "confidence": "HIGH"}]


def report(statement: str):
    return tool_use("submit_report", {**REPORT_ARGS, "findings": findings(statement)})


def mission_report(summary: str):
    return tool_use("submit_mission_report", {
        "executive_summary": summary, "objective_status": "ACHIEVED",
        "key_findings": [{"kind": "RECOMMENDATION", "statement": "Proceed", "confidence": "MEDIUM"}],
    })


NO_FOLLOWUPS = tool_use("request_followups", {"assessment": "complete", "tasks": []})


def make_live(registry: AgentRegistry, llm: FakeLLM, store: WorldStore | None = None) -> tuple[WorldStore, LiveEngine]:
    store = store or WorldStore(registry, EventBus())
    engine = LiveEngine(
        store, loader=AgentLoader(registry, MODELS),
        context=NodeContext(registry, local_dir=Path("/nonexistent-atlas-local")),
        config=LiveConfig(models=MODELS, max_turns=5), llm=llm, prices=PriceTable(),
    )
    return store, engine


async def until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.001)


def calls_matching(llm: FakeLLM, pred) -> list[dict]:
    return [c for c in llm.calls if pred(c)]


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def base_llm() -> FakeLLM:
    """Round 1: research (sofia) → analysis (oracle); closes with report v1."""
    llm = FakeLLM()
    llm.when(stage("PLANNING"), tool_use("create_plan", {"tasks": [
        t("research", "sofia", "Market research"), t("analysis", "oracle", "Scenario analysis", ["research"]),
    ]}))
    llm.when(task_call("Market research"), report("Average rent is 18k MXN"))
    llm.when(task_call("Scenario analysis"), report("Base case IRR 14%"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report("Round one summary"))
    return llm


def thread(snap: WorldState, mission_id: str) -> list[tuple[str, str, str]]:
    return [(m.from_agent, m.to_agent, m.type) for m in snap.messages
            if m.mission_id == mission_id and HUMAN in (m.from_agent, m.to_agent)]


# ---------------------------------------------------------------------------
# running mission: human guidance
# ---------------------------------------------------------------------------


def test_message_while_running_reaches_later_tasks_and_atlas_steps(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), tool_use("create_plan", {"tasks": [
        t("research", "sofia", "Market research"), t("analysis", "oracle", "Scenario analysis", ["research"]),
    ]}))
    llm.when(task_call("Market research"),
             tool_use("request_approval", {"reason": "INSUFFICIENT_INFORMATION", "title": "Which year?",
                                           "detail": "prices of which year?"}),
             report("Average rent is 18k MXN"))
    llm.when(task_call("Scenario analysis"), report("Base case IRR 14%"))
    llm.when(stage("ACKNOWLEDGE"), tool_use("acknowledge", {"text": "Understood: 2025 prices from now on."}))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report("Done"))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("Evaluate the Polanco 2BR", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        msg = await engine.post_message(m.id, "Use 2025 prices only")
        assert msg.from_agent == HUMAN and msg.to_agent == "atlas" and msg.type == "REQUEST"
        await until(lambda: len(thread(store.snapshot(), m.id)) == 2)
        await store.decide_approval(store.snapshot().approvals[0].id, "APPROVED")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store, m.id

    store, mid = asyncio.run(go())
    snap = store.snapshot()
    assert thread(snap, mid) == [(HUMAN, "atlas", "REQUEST"), ("atlas", HUMAN, "ANSWER")]
    ack = next(m for m in snap.messages if m.to_agent == HUMAN)
    assert ack.body == "Understood: 2025 prices from now on." and ack.in_reply_to == snap.messages[0].id
    assert calls_matching(llm, stage("ACKNOWLEDGE"))[0]["model"] == "claude-haiku-test"

    # the task that started before the note did not get it; the later task, the review and the report did
    first = call_text(calls_matching(llm, task_call("Market research"))[0])
    assert "Use 2025 prices only" not in first
    later = call_text(calls_matching(llm, task_call("Scenario analysis"))[0])
    assert "Human guidance" in later and "Use 2025 prices only" in later
    assert "Use 2025 prices only" in call_text(calls_matching(llm, stage("REVIEW"))[0])
    assert "Use 2025 prices only" in call_text(calls_matching(llm, stage("CONSOLIDATION"))[0])
    # the stored task is unchanged (guidance is prompt-only)
    assert "2025" not in next(x for x in snap.tasks if x.title == "Scenario analysis").description
    assert snap.missions[0].phase == "CLOSED" and snap.missions[0].round == 1


def test_ack_falls_back_when_the_model_fails(registry):
    llm = base_llm()
    llm.rules[1] = (task_call("Market research"), [
        tool_use("request_approval", {"reason": "INSUFFICIENT_INFORMATION", "title": "Q", "detail": "d"}),
        report("Average rent is 18k MXN")], False)

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        await engine.post_message(m.id, "Be brief")
        await until(lambda: len(thread(store.snapshot(), m.id)) == 2)
        await store.decide_approval(store.snapshot().approvals[0].id, "APPROVED")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    assert "Noted" in next(m for m in snap.messages if m.to_agent == HUMAN).body


# ---------------------------------------------------------------------------
# closed mission: answer only / follow-up round
# ---------------------------------------------------------------------------


def test_message_after_close_answer_only(registry):
    llm = base_llm()
    llm.when(stage("FOLLOW-UP"), tool_use("respond_to_followup", {"answer": "The base-case IRR is 14%."}))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("Evaluate the Polanco 2BR", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        n_events = store.bus.last_seq
        await engine.post_message(m.id, "What was the IRR?")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store, m.id, n_events

    store, mid, n_events = asyncio.run(go())
    snap = store.snapshot()
    assert thread(snap, mid) == [(HUMAN, "atlas", "REQUEST"), ("atlas", HUMAN, "ANSWER")]
    answer = next(m for m in snap.messages if m.to_agent == HUMAN)
    assert answer.body == "The base-case IRR is 14%." and answer.in_reply_to == snap.messages[0].id
    mission = snap.missions[0]
    assert mission.round == 1 and mission.phase == "CLOSED" and len(snap.tasks) == 2
    assert [r.version for r in snap.mission_reports] == [1]
    assert not [e for e in store.bus.history(n_events) if e.type in ("mission.phase_changed", "task.created")]
    prompt = call_text(calls_matching(llm, stage("FOLLOW-UP"))[0])
    assert "What was the IRR?" in prompt and "Round one summary" in prompt and "Base case IRR 14%" in prompt
    assert "T1 · Market research" in prompt
    assert calls_matching(llm, stage("FOLLOW-UP"))[0]["model"] == "claude-opus-test"


def test_message_after_close_opens_round_two(registry):
    llm = base_llm()
    llm.when(stage("FOLLOW-UP"), tool_use("respond_to_followup", {
        "answer": "I'll dig deeper into financing.",
        "tasks": [t("fin", "sofia", "Financing options", ["T1"]),
                  t("model", "oracle", "Update the model", ["fin", "T2"])],
    }))
    llm.when(task_call("Financing options"), report("Mortgage at 11%"))
    llm.when(task_call("Update the model"), report("IRR with financing 17%"))
    llm.when(stage("CONSOLIDATION"), mission_report("Round two summary"))

    async def go():
        store, engine = make_live(registry, llm)
        initial = store.snapshot()
        m = await engine.start("Evaluate the Polanco 2BR", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        v1 = store.snapshot().missions[0].final_report_id
        mark = store.bus.last_seq
        await engine.post_message(m.id, "Add financing scenarios")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store, initial, m.id, v1, mark

    store, initial, mid, v1, mark = asyncio.run(go())
    snap = store.snapshot()
    mission = snap.missions[0]
    assert mission.round == 2 and mission.phase == "CLOSED" and not mission.interrupted
    new = [x for x in snap.tasks if x.round == 2]
    assert [x.title for x in new] == ["Financing options", "Update the model"]
    assert all(x.status == "COMPLETED" for x in snap.tasks)
    assert [x.round for x in snap.tasks[:2]] == [1, 1]
    research, analysis = snap.tasks[0], snap.tasks[1]
    assert new[0].depends_on == [research.id] and new[1].depends_on == [new[0].id, analysis.id]

    # report versions: v1 kept, v2 is the mission's final report
    versions = {r.version: r for r in snap.mission_reports}
    assert set(versions) == {1, 2} and versions[1].id == v1
    assert mission.final_report_id == versions[2].id
    assert versions[2].executive_summary == "Round two summary"
    assert set(versions[2].tasks_completed) == {"Market research", "Scenario analysis", "Financing options",
                                                "Update the model"}

    # phases of round 2, and the thread
    phases = [e.payload["mission"]["phase"] for e in store.bus.history(mark)
              if e.type in ("mission.phase_changed", "mission.closed")]
    assert phases == ["DELEGATION", "EXECUTION", "VALIDATION", "CONSOLIDATION", "REPORTING", "FOLLOW_UP",
                      "CLOSED"]
    assert any("Round 2 started" in e.summary for e in store.bus.history(mark))
    assert thread(snap, mid) == [(HUMAN, "atlas", "REQUEST"), ("atlas", HUMAN, "ANSWER"),
                                 ("atlas", HUMAN, "RESULT")]
    assert "report v2" in snap.messages[-1].body

    # prompts: dependencies on round-1 reports, the request in tasks and consolidation
    fin = call_text(calls_matching(llm, task_call("Financing options"))[0])
    assert "Average rent is 18k MXN" in fin and "Add financing scenarios" in fin
    upd = call_text(calls_matching(llm, task_call("Update the model"))[0])
    assert "Mortgage at 11%" in upd and "Base case IRR 14%" in upd
    cons = call_text(calls_matching(llm, stage("CONSOLIDATION"))[1])
    assert "follow-up round 2" in cons and "Add financing scenarios" in cons
    assert len(calls_matching(llm, stage("REVIEW"))) == 1  # no review round in follow-ups

    # agents released, reducer reproduces the snapshot
    assert {s.status for s in snap.agent_states} <= {"IDLE", "MONITORING"}
    assert fold(initial, store.bus.history()).model_dump(mode="json") == snap.model_dump(mode="json")


def test_invalid_followup_is_retried_then_reported(registry):
    llm = base_llm()
    llm.when(stage("FOLLOW-UP"),
             tool_use("respond_to_followup", {"tasks": [t("x", "nobody", "X")]}),
             tool_use("respond_to_followup", {"tasks": [t("x", "sofia", "X", ["T9"])]}))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        await engine.post_message(m.id, "More")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    assert snap.missions[0].round == 1 and len(snap.tasks) == 2
    assert "could not turn this follow-up" in snap.messages[-1].body
    assert len(calls_matching(llm, stage("FOLLOW-UP"))) == 2


# ---------------------------------------------------------------------------
# interrupted mission resumed through the thread
# ---------------------------------------------------------------------------


def test_interrupted_mission_resumed_via_message(registry, tmp_path: Path):
    path = tmp_path / "atlas.db"
    llm = FakeLLM()
    llm.when(stage("PLANNING"), tool_use("create_plan", {"tasks": [
        t("research", "sofia", "Market research"), t("send", "alfred", "Send offer", ["research"]),
    ]}))
    llm.when(task_call("Market research"), report("Rent is 18k"))
    llm.when(task_call("Send offer"), tool_use("request_approval", {
        "reason": "EXTERNAL_COMMUNICATION", "title": "Send", "detail": "d"}))

    async def first():
        store, engine = make_live(registry, llm, WorldStore(registry, EventBus(log=EventLog(path))))
        m = await engine.start("Buy it", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        await engine.stop_all()
        return m.id

    mid = asyncio.run(first())

    llm2 = FakeLLM()
    llm2.when(stage("FOLLOW-UP"), tool_use("respond_to_followup", {
        "answer": "Resuming: I'll redo the offer.", "tasks": [t("offer", "alfred", "Draft the offer", ["T1"])]}))
    llm2.when(task_call("Draft the offer"), report("Offer drafted"))
    llm2.when(stage("CONSOLIDATION"), mission_report("Resumed and done"))

    async def second():
        store = WorldStore(registry, EventBus(log=EventLog(path)))
        store.restore()
        await store.recover_interrupted()
        assert store.mission(mid).interrupted
        store, engine = make_live(registry, llm2, store)
        await engine.post_message(mid, "Please resume")
        await asyncio.wait_for(engine.wait(mid), 3)
        return store

    snap = asyncio.run(second()).snapshot()
    mission = snap.missions[0]
    assert mission.round == 2 and mission.phase == "CLOSED" and not mission.interrupted
    assert [(x.title, x.status, x.round) for x in snap.tasks] == [
        ("Market research", "COMPLETED", 1), ("Send offer", "CANCELLED", 1), ("Draft the offer", "COMPLETED", 2)]
    assert [r.version for r in snap.mission_reports] == [2]
    prompt = call_text(calls_matching(llm2, stage("FOLLOW-UP"))[0])
    assert "interrupted" in prompt and "Please resume" in prompt
    assert "Rent is 18k" in call_text(calls_matching(llm2, task_call("Draft the offer"))[0])


# ---------------------------------------------------------------------------
# subscription backend (FakeClaudeSDK)
# ---------------------------------------------------------------------------


def sdk_stage(name: str):
    return lambda s: f"STAGE: {name}" in s.prompt


def sdk_task(title: str):
    return lambda s: f"Your task: {title}" in s.prompt


def sdk_report(statement: str) -> tuple:
    return call("submit_report", {**REPORT_ARGS, "findings": findings(statement)})


def test_subscription_guidance_and_followup_round(registry):
    sdk = FakeClaudeSDK()
    sdk.when(sdk_stage("PLANNING"), [call("create_plan", {"tasks": [
        t("research", "sofia", "Market research"), t("analysis", "oracle", "Scenario analysis", ["research"]),
    ]}), say("done")])
    sdk.when(sdk_task("Market research"), [
        call("request_approval", {"reason": "INSUFFICIENT_INFORMATION", "title": "Q", "detail": "d"}),
        sdk_report("Rent 18k"), say("done")])
    sdk.when(sdk_task("Scenario analysis"), [sdk_report("IRR 14%"), say("done")])
    sdk.when(sdk_stage("ACKNOWLEDGE"), [call("acknowledge", {"text": "Got it."}), say("done")])
    sdk.when(sdk_stage("REVIEW"), [call("request_followups", {"assessment": "ok", "tasks": []}), say("done")])
    sdk.when(sdk_stage("CONSOLIDATION"), [call("submit_mission_report", {
        "executive_summary": "v1", "objective_status": "ACHIEVED", "key_findings": []}), say("done")],
        [call("submit_mission_report", {
            "executive_summary": "v2", "objective_status": "ACHIEVED", "key_findings": []}), say("done")])
    sdk.when(sdk_stage("FOLLOW-UP"), [call("respond_to_followup", {
        "answer": "On it.", "tasks": [t("x", "sofia", "Extra check", ["T2"]), t("y", "argos", "Audit", ["x"])],
    }), say("done")])
    sdk.when(sdk_task("Extra check"), [sdk_report("Checked"), say("done")])
    sdk.when(sdk_task("Audit"), [sdk_report("Audited"), say("done")])

    async def go():
        store = WorldStore(registry, EventBus())
        engine = LiveEngine(
            store, loader=AgentLoader(registry, MODELS),
            context=NodeContext(registry, local_dir=Path("/nonexistent-atlas-local")),
            config=LiveConfig(models=MODELS, max_turns=8), prices=PriceTable(), backend="subscription",
            sdk_query=sdk,
        )
        m = await engine.start("Evaluate", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        await engine.post_message(m.id, "Focus on rentals")
        await until(lambda: len(thread(store.snapshot(), m.id)) == 2)
        await store.decide_approval(store.snapshot().approvals[0].id, "APPROVED")
        await asyncio.wait_for(engine.wait(m.id), 3)
        await engine.post_message(m.id, "Double-check the analysis")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store, m.id

    store, _mid = asyncio.run(go())
    snap = store.snapshot()
    assert snap.missions[0].round == 2 and snap.missions[0].phase == "CLOSED"
    assert [x.round for x in snap.tasks] == [1, 1, 2, 2] and all(x.status == "COMPLETED" for x in snap.tasks)
    assert sorted(r.version for r in snap.mission_reports) == [1, 2]
    assert [m.body for m in snap.messages if m.to_agent == HUMAN][:2] == ["Got it.", "On it."]
    later = sdk.matching(sdk_task("Scenario analysis"))[0].prompt
    assert "Focus on rentals" in later
    assert "Focus on rentals" not in sdk.matching(sdk_task("Market research"))[0].prompt
    extra = sdk.matching(sdk_task("Extra check"))[0].prompt
    assert "IRR 14%" in extra and "Double-check the analysis" in extra
    assert "mcp__atlas__respond_to_followup" in sdk.matching(sdk_stage("FOLLOW-UP"))[0].options.allowed_tools


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


SIM = {
    "id": "thread-sim", "node": "corporate", "title": "t", "objective": "o",
    "steps": [{"do": "phase", "phase": "DECOMPOSITION"}, {"do": "phase", "phase": "CLOSED"}],
}


def test_http_thread(monkeypatch):
    from atlas.main import app
    from atlas.sim.scenario import Scenario

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        app.state.sim.library.directory = None
        app.state.sim.library.add(Scenario.model_validate(SIM))
        sim_id = client.post("/missions", json={"objective": "s", "node": "corporate",
                                                "scenario_id": "thread-sim", "speed": 1000}).json()["id"]
        r = client.post(f"/missions/{sim_id}/messages", json={"text": "Can you go deeper?"})
        assert r.status_code == 200
        body = r.json()
        assert body["from"] == HUMAN and body["to"] == "atlas" and body["type"] == "REQUEST"
        msgs = client.get("/state").json()["messages"]
        assert msgs[-1]["from"] == "atlas" and msgs[-1]["body"] == SIMULATED_REPLY
        assert msgs[-1]["in_reply_to"] == body["id"]
        assert client.post(f"/missions/{sim_id}/messages", json={"text": "  "}).status_code == 422
        assert client.post("/missions/msn_nope/messages", json={"text": "hi"}).status_code == 404

        # a closed live mission needs a usable backend for a follow-up
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm = base_llm()
        llm.when(stage("FOLLOW-UP"), tool_use("respond_to_followup", {"answer": "IRR 14%."}))
        app.state.live.llm = llm
        r = client.post("/missions", json={"objective": "Live", "node": "corporate"})
        assert r.status_code == 200, r.text
        live_id = r.json()["id"]
        deadline = time.monotonic() + 3
        while client.get("/missions").json()[0]["phase"] != "CLOSED":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        r = client.post(f"/missions/{live_id}/messages", json={"text": "IRR?"})
        assert r.status_code == 422 and "unavailable" in r.json()["detail"]
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        r = client.post(f"/missions/{live_id}/messages", json={"text": "IRR?"})
        assert r.status_code == 200
        deadline = time.monotonic() + 3
        while client.get("/state").json()["messages"][-1]["body"] != "IRR 14%.":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        hist = client.get("/missions").json()[0]
        assert hist["id"] == live_id and hist["report_versions"] == [1]
