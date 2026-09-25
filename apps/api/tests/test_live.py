"""Live engine tests (Phase 2) — FakeLLM only, no network."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core.events import EventBus
from atlas.core.models import Mission, WorldState
from atlas.core.registry import DEFAULT_AGENTS_DIR, AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.live import (
    AgentLoader,
    FakeLLM,
    LiveConfig,
    LiveEngine,
    LLMError,
    ModelConfig,
    NodeContext,
    PriceTable,
    validate_plan,
)
from atlas.live.agent_loader import parse_frontmatter
from atlas.live.llm import FakeUsage, Meter, call_text, text, tool_use
from atlas.live.pricing import _parse

MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def stage(name: str):
    return lambda kw: f"STAGE: {name}" in call_text(kw)


def task_call(title: str):
    return lambda kw: f"Your task: {title}" in call_text(kw)


def is_consult(kw) -> bool:
    return "(consultation)" in call_text(kw)


def plan(*tasks: dict) -> object:
    return tool_use("create_plan", {"rationale": "split the work", "tasks": list(tasks)})


def t(ref: str, agent: str, title: str, deps: list[str] | None = None, **kw) -> dict:
    return {"ref": ref, "title": title, "description": f"Do {title.lower()}", "assigned_to": agent,
            "depends_on": deps or [], **kw}


def report(statement: str, kind: str = "FACT", **kw):
    return tool_use("submit_report", {
        "asked_to": "the task", "actions_taken": ["worked"], "inputs_used": ["context"],
        "findings": [{"kind": kind, "statement": statement, "sources": ["ctx"], "confidence": "HIGH"}],
        "confidence": "HIGH", **kw,
    })


NO_FOLLOWUPS = tool_use("request_followups", {"assessment": "complete", "tasks": []})


def mission_report(summary: str = "All good.", status: str = "ACHIEVED"):
    return tool_use("submit_mission_report", {
        "executive_summary": summary, "objective_status": status,
        "key_findings": [{"kind": "RECOMMENDATION", "statement": "Proceed", "confidence": "MEDIUM"}],
        "next_actions": ["Decide"],
    })


def make_live(registry: AgentRegistry, llm: FakeLLM, local: Path | None = None, **cfg) -> tuple[WorldStore, LiveEngine]:
    store = WorldStore(registry, EventBus())
    config = LiveConfig(models=MODELS, **({"max_turns": 5, "audit": False, "publish": False, "task_retries": 0} | cfg))
    engine = LiveEngine(
        store,
        loader=AgentLoader(registry, MODELS),
        context=NodeContext(registry, local_dir=local or Path("/nonexistent-atlas-local")),
        config=config,
        llm=llm,
        prices=PriceTable(),
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


@pytest.fixture
def two_nodes(tmp_path: Path, monkeypatch) -> AgentRegistry:
    """Agents with the personal node enabled and every EOS claude_md file present."""
    shutil.copytree(DEFAULT_AGENTS_DIR, tmp_path / "agents")
    org = tmp_path / "agents" / "organization.yaml"
    org.write_text(org.read_text().replace("enabled: false", "enabled: true"))
    md = tmp_path / "claude-agents"
    md.mkdir()
    for name in ("eos-data", "eos-issues", "eos-people", "eos-process", "eos-traction", "eos-vision"):
        (md / f"{name}.md").write_text(f"---\nname: {name}\nmodel: sonnet\n---\nYou are {name}.\n")
    monkeypatch.setenv("ATLAS_CLAUDE_AGENTS_DIR", str(md))
    return AgentRegistry.load(tmp_path / "agents")


@pytest.fixture
def local_ctx(tmp_path: Path) -> Path:
    local = tmp_path / "atlas-local"
    (local / "context" / "corporate" / "eos").mkdir(parents=True)
    (local / "context" / "personal" / "notes").mkdir(parents=True)
    (local / "context" / "corporate" / "company.md").write_text("CORP-SECRET: margins are 12%")
    (local / "context" / "corporate" / "eos" / "scorecard.md").write_text("EOS-SECRET: scorecard")
    (local / "context" / "corporate" / "ignored.pdf").write_text("not text")
    (local / "context" / "personal" / "me.md").write_text("PERSONAL-SECRET: family budget")
    (local / "context" / "personal" / "notes" / "trip.txt").write_text("PERSONAL-TRIP: Oaxaca")
    try:  # a symlink escaping the node folder must be ignored
        os.symlink(local / "context" / "corporate" / "company.md", local / "context" / "personal" / "leak.md")
    except OSError:
        pass
    return local


# ---------------------------------------------------------------------------
# full mission
# ---------------------------------------------------------------------------


def full_mission_llm() -> FakeLLM:
    llm = FakeLLM()
    llm.when(is_consult, text("Use a 10% discount rate (ASSUMPTION)."))
    llm.when(stage("PLANNING"), plan(
        t("research", "sofia", "Market research", priority="HIGH"),
        t("check", "argos", "Consistency check"),
        t("analysis", "oracle", "Scenario analysis", ["research", "check"], requires_approval=True,
          approval_reason="FINANCIAL_COMMITMENT"),
    ))
    llm.when(task_call("Market research"),
             tool_use("consult", {"agent_id": "oracle", "question": "Which discount rate should I use?"}),
             report("Average rent is 18k MXN"))
    llm.when(task_call("Consistency check"), report("Pro-forma matches data room", kind="ASSUMPTION"))
    llm.when(task_call("Scenario analysis"),
             tool_use("request_approval", {"reason": "FINANCIAL_COMMITMENT", "title": "Commit deposit",
                                           "detail": "Reserve the unit", "proposed_action": "Pay 50k"}),
             report("Base case IRR 14%", kind="SCENARIO"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())
    return llm


def test_full_live_mission(registry):
    llm = full_mission_llm()

    async def go():
        store, engine = make_live(registry, llm, web_search=True)
        initial = store.snapshot()
        mission = await engine.start("Evaluate the Polanco 2BR", "corporate")
        assert mission.mode == "live"
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        snap = store.snapshot()
        apr = snap.approvals[0]
        analysis = next(x for x in snap.tasks if x.title == "Scenario analysis")
        oracle = next(s for s in snap.agent_states if s.agent_id == "oracle")
        assert analysis.status == "AWAITING_APPROVAL" and apr.task_id == analysis.id
        assert oracle.status == "WAITING" and oracle.activity == "Awaiting human approval"
        await store.decide_approval(apr.id, "APPROVED", note="go ahead")
        await asyncio.wait_for(engine.wait(mission.id), 3)
        return store, engine, initial, mission.id

    store, engine, initial, mid = asyncio.run(go())
    snap = store.snapshot()
    mission = next(m for m in snap.missions if m.id == mid)
    assert mission.phase == "CLOSED" and mission.mode == "live"
    assert engine.running() == []

    tasks = {x.title: x for x in snap.tasks}
    assert {x.status for x in tasks.values()} == {"COMPLETED"}
    assert tasks["Scenario analysis"].depends_on == [tasks["Market research"].id, tasks["Consistency check"].id]
    assert tasks["Scenario analysis"].requires_approval
    assert len(snap.agent_reports) == 3
    assert all(x.result_report_id for x in tasks.values())

    # the two independent tasks ran in parallel
    events = store.bus.history()
    upd = [e for e in events if e.type == "task.updated"]
    started = [i for i, e in enumerate(upd) if e.payload["task"]["status"] == "IN_PROGRESS"
               and e.payload["task"]["title"] in ("Market research", "Consistency check")]
    first_done = next(i for i, e in enumerate(upd) if e.payload["task"]["status"] == "COMPLETED")
    assert len(started) >= 2 and max(started[:2]) < first_done

    # consult → REQUEST + ANSWER, both COLLABORATING, phase COLLABORATION
    msgs = [(m.from_agent, m.to_agent, m.type) for m in snap.messages]
    assert msgs == [("sofia", "oracle", "REQUEST"), ("oracle", "sofia", "ANSWER")]
    assert snap.messages[1].in_reply_to == snap.messages[0].id
    collab = {(e.payload["state"]["agent_id"]) for e in events if e.type == "agent.state_changed"
              and e.payload["state"]["status"] == "COLLABORATING"}
    assert collab == {"sofia", "oracle"}
    phases = [e.payload["mission"]["phase"] for e in events
              if e.type in ("mission.phase_changed", "mission.closed")]
    assert phases == ["DECOMPOSITION", "DELEGATION", "EXECUTION", "COLLABORATION", "VALIDATION",
                      "CONSOLIDATION", "REPORTING", "FOLLOW_UP", "CLOSED"]
    consult = calls_matching(llm, is_consult)[0]
    assert consult["model"] == "claude-haiku-test" and "tools" not in consult

    # approval decision + note come back as the tool result
    oracle_calls = calls_matching(llm, task_call("Scenario analysis"))
    assert len(oracle_calls) == 2
    assert "APPROVED" in call_text(oracle_calls[1]) and "go ahead" in call_text(oracle_calls[1])
    # the dependent task sees its dependencies' reports (and only those)
    first = call_text(oracle_calls[0])
    assert "Average rent is 18k MXN" in first and "Pro-forma matches data room" in first
    assert "Market research" not in call_text(calls_matching(llm, task_call("Consistency check"))[0]).split(
        "Your task:")[1]

    # prompts: cache breakpoint on the last system block, web search only for SOFIA
    sofia_call = calls_matching(llm, task_call("Market research"))[0]
    assert sofia_call["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert any(tl.get("type", "").startswith("web_search") for tl in sofia_call["tools"])
    argos_call = calls_matching(llm, task_call("Consistency check"))[0]
    assert not any(tl.get("type", "").startswith("web_search") for tl in argos_call["tools"])
    assert sofia_call["model"] == "claude-sonnet-test"
    assert calls_matching(llm, stage("PLANNING"))[0]["model"] == "claude-opus-test"

    # mission report + usage
    final = snap.mission_reports[0]
    assert mission.final_report_id == final.id
    assert final.objective_status == "ACHIEVED" and final.key_findings[0].kind == "RECOMMENDATION"
    assert sorted(final.tasks_completed) == ["Consistency check", "Market research", "Scenario analysis"]
    assert len(final.agent_report_ids) == 3
    kinds = {c.kind for r in snap.agent_reports for c in r.findings}
    assert kinds == {"FACT", "ASSUMPTION", "SCENARIO"}
    assert mission.usage.llm_calls == len(llm.calls) == 9
    assert mission.usage.input_tokens == 100 * 9 and mission.usage.output_tokens == 50 * 9
    assert mission.usage.est_cost_usd > 0
    assert len([e for e in events if e.type == "mission.updated"]) == 9

    # agents released, reducer reproduces the snapshot
    states = {s.agent_id: s for s in snap.agent_states}
    assert states["argos"].status == "MONITORING"
    for a in ("atlas", "sofia", "oracle"):
        assert states[a].status == "IDLE" and states[a].current_task_id is None
    folded = fold(initial, store.bus.history())
    assert folded.model_dump(mode="json") == snap.model_dump(mode="json")


def test_followup_round(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B")))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), tool_use("request_followups", {"assessment": "gap", "tasks": [
        t("c", "oracle", "Resolve conflict", ["a", "b"])]}))
    llm.when(task_call("Resolve conflict"), report("C"))
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    assert [x.status for x in snap.tasks] == ["COMPLETED"] * 3
    c = next(x for x in snap.tasks if x.title == "Resolve conflict")
    assert len(c.depends_on) == 2
    assert len(calls_matching(llm, stage("REVIEW"))) == 1  # one follow-up round only
    assert "## Report · Research A" in call_text(calls_matching(llm, task_call("Resolve conflict"))[0])


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_validate_plan():
    allowed = {"sofia", "oracle"}
    ok, errs = validate_plan({"tasks": [t("b", "oracle", "B", ["a"]), t("a", "sofia", "A")]}, allowed)
    assert not errs and [x["ref"] for x in ok] == ["a", "b"]  # topological order
    _, errs = validate_plan({"tasks": [t("a", "sofia", "A", ["b"]), t("b", "oracle", "B", ["a"])]}, allowed)
    assert errs and "cycle" in errs[0]
    _, errs = validate_plan({"tasks": [t("a", "sofia", "A"), t("a", "eos-data", "B", ["zz"])]}, allowed)
    assert any("duplicate" in e for e in errs) and any("not in the roster" in e for e in errs)
    assert any("unknown ref 'zz'" in e for e in errs)
    _, errs = validate_plan({"tasks": [t("a", "sofia", "A")]}, allowed)
    assert "between 2 and 8" in errs[0]
    ok, errs = validate_plan({"tasks": [t("x", "sofia", "X", ["a"], requires_approval=True)]}, allowed,
                             existing_refs={"a"}, min_tasks=0, max_tasks=3)
    assert not errs and ok[0]["approval_reason"] == "CONSEQUENTIAL_DECISION"


def test_plan_validation_retry(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"),
             plan(t("a", "sofia", "Research A"), t("b", "eos-data", "Scorecard", ["a"])),  # eos-data unavailable
             plan(t("a", "sofia", "Research A"), t("b", "oracle", "Analysis B", ["a"])))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Analysis B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    store = asyncio.run(go())
    snap = store.snapshot()
    assert [x.assigned_to for x in snap.tasks] == ["sofia", "oracle"]
    assert snap.missions[0].phase == "CLOSED"
    retry = calls_matching(llm, stage("PLANNING"))[1]
    last = retry["messages"][-1]["content"][0]
    assert last["type"] == "tool_result" and last["is_error"] and "not in the roster" in last["content"]
    assert any("plan rejected (retrying)" in e.summary for e in store.bus.history())
    # the roster offered to ATLAS never includes unavailable agents
    plan_tool = retry["tools"][0]
    assert "eos-data" not in plan_tool["input_schema"]["properties"]["tasks"]["items"]["properties"][
        "assigned_to"]["enum"]


def test_plan_invalid_twice_closes_mission(registry):
    bad = plan(t("a", "sofia", "A", ["a"]), t("b", "oracle", "B"))
    llm = FakeLLM().when(stage("PLANNING"), bad, bad)

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    assert snap.tasks == [] and snap.missions[0].phase == "CLOSED"
    assert snap.mission_reports[0].objective_status == "NOT_ACHIEVED"
    assert all(s.status in ("IDLE", "MONITORING") for s in snap.agent_states)


# ---------------------------------------------------------------------------
# failures, fallbacks, cancel
# ---------------------------------------------------------------------------


def test_llm_failure_fails_task_and_mission_closes(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B"),
                                     t("c", "oracle", "Analysis C", ["b"])))
    llm.when(task_call("Research A"), text("Here are my notes without a report."), text("Still no report."))
    llm.when(task_call("Check B"), LLMError("OverloadedError: overloaded"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), LLMError("boom"))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await until(lambda: any(s.agent_id == "argos" and s.status == "ERROR"
                                for s in store.snapshot().agent_states))
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    store = asyncio.run(go())
    snap = store.snapshot()
    status = {x.title: x.status for x in snap.tasks}
    assert status == {"Research A": "COMPLETED", "Check B": "FAILED", "Analysis C": "CANCELLED"}
    # SOFIA never submitted: its final text was wrapped into a low-confidence report
    wrapped = snap.agent_reports[0]
    assert wrapped.confidence == "LOW" and wrapped.findings[0].kind == "ASSUMPTION"
    assert "Still no report." in wrapped.findings[0].statement
    sofia_calls = calls_matching(llm, task_call("Research A"))
    assert sofia_calls[1]["tool_choice"] == {"type": "tool", "name": "submit_report"}  # nudged once
    final = snap.mission_reports[0]
    assert snap.missions[0].phase == "CLOSED"
    assert any("Check B" in x and "failed" in x for x in final.needs_human_attention)
    assert "Check B" in final.tasks_pending and final.objective_status == "PARTIAL"
    assert {s.agent_id: s.status for s in snap.agent_states}["argos"] == "MONITORING"


def test_approval_required_before_report(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "alfred", "Send memo", requires_approval=True,
                                       approval_reason="EXTERNAL_COMMUNICATION"), t("b", "sofia", "Research")))
    llm.when(task_call("Send memo"), report("Memo sent"),
             tool_use("request_approval", {"reason": "EXTERNAL_COMMUNICATION", "title": "Send memo",
                                           "detail": "Email the memo"}),
             report("Memo held"))
    llm.when(task_call("Research"), report("R"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        await store.decide_approval(store.snapshot().approvals[0].id, "REJECTED", note="not yet")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    calls = calls_matching(llm, task_call("Send memo"))
    assert "call request_approval first" in call_text(calls[1])
    assert "REJECTED" in call_text(calls[2]) and "not yet" in call_text(calls[2])
    assert snap.approvals[0].state == "REJECTED"
    assert next(r for r in snap.agent_reports if r.agent_id == "alfred").findings[0].statement == "Memo held"


def test_cancel_live_mission(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "alfred", "Send memo", requires_approval=True),
                                     t("b", "oracle", "Follow", ["a"])))
    llm.when(task_call("Send memo"), tool_use("request_approval", {"reason": "EXTERNAL_COMMUNICATION",
                                                                   "title": "Send", "detail": "d"}))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        assert await engine.cancel(m.id)
        assert not await engine.cancel(m.id)
        return store, engine

    store, engine = asyncio.run(go())
    snap = store.snapshot()
    assert snap.missions[0].phase == "CLOSED" and engine.running() == []
    assert {x.status for x in snap.tasks} == {"CANCELLED"}
    assert snap.approvals[0].state == "EXPIRED"
    assert all(s.status in ("IDLE", "MONITORING") and not s.current_task_id for s in snap.agent_states)
    assert any("Mission cancelled" in e.summary for e in store.bus.history())


# ---------------------------------------------------------------------------
# usage + pricing
# ---------------------------------------------------------------------------


def test_usage_accumulation_and_prices(registry, monkeypatch):
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
             "cache_creation_input_tokens": 100}
    llm = FakeLLM([text("a", **usage), text("b", **usage)])

    async def go():
        store = WorldStore(registry, EventBus())
        m = await store.create_mission("x", "corporate", mode="live")
        meter = Meter(llm, store, m.id, PriceTable())
        await meter.create(model="claude-sonnet-5", messages=[])
        await meter.create(model="claude-sonnet-5", messages=[])
        return store, m.id

    store, mid = asyncio.run(go())
    u = store.mission(mid).usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.llm_calls) == (2200, 1000, 4000, 2)
    one = (1000 * 3 + 500 * 15 + 2000 * 0.3 + 100 * 3.75) / 1e6
    assert u.est_cost_usd == pytest.approx(2 * one)
    ev = [e for e in store.bus.history() if e.type == "mission.updated"]
    assert len(ev) == 2 and ev[-1].payload["mission"]["usage"]["llm_calls"] == 2

    prices = PriceTable()
    assert prices.price_for("claude-opus-5-5").input == 5 and prices.price_for("claude-haiku-4-5").output == 5
    assert prices.price_for("mystery").input == 3  # sonnet-class fallback
    custom = PriceTable(_parse('{"claude-sonnet-5": {"input": 2, "output": 10}, "haiku": [0.5, 2]}'))
    assert custom.price_for("claude-sonnet-5").cache_read == pytest.approx(0.2)
    assert custom.price_for("claude-haiku-9").input == 0.5
    assert _parse("not json") == {}
    assert FakeUsage().input_tokens == 100


# ---------------------------------------------------------------------------
# agent sources
# ---------------------------------------------------------------------------


def test_claude_md_loading(registry, tmp_path, monkeypatch):
    md = tmp_path / "agents-md"
    md.mkdir()
    (md / "eos-data.md").write_text("---\nname: eos-data\nmodel: opus\ntools: Read\n---\n\nYou are the data seat.\n")
    (md / "eos-issues.md").write_text("---\nmodel: inherit\n---\nIssues prompt")
    (md / "eos-people.md").write_text("No frontmatter at all")
    monkeypatch.setenv("ATLAS_CLAUDE_AGENTS_DIR", str(md))
    loader = AgentLoader(registry, MODELS)

    data = loader.resolve("eos-data")
    assert data.available and data.source == "claude_md"
    assert data.role_prompt == "You are the data seat." and data.model == "claude-opus-test"
    assert loader.resolve("eos-issues").model == "claude-sonnet-test"
    assert loader.resolve("eos-people").role_prompt == "No frontmatter at all"
    vision = loader.resolve("eos-vision")  # file missing
    assert not vision.available and "cannot read" in (vision.reason or "")

    avail = loader.availability()
    assert avail["eos-data"] == {"available": True}
    assert avail["eos-vision"]["available"] is False
    roster = {r.id for r in loader.roster("corporate")}
    assert {"eos-data", "eos-issues", "eos-people", "sofia"} <= roster
    assert "eos-vision" not in roster and "atlas" not in roster

    sofia = loader.resolve("sofia")  # claude adapter: YAML prompt or generic fallback
    assert sofia.available and sofia.source in ("yaml", "generic") and sofia.role_prompt
    assert parse_frontmatter("---\nmodel: haiku\n---\nbody") == ({"model": "haiku"}, "body")


def test_unset_env_falls_back_to_default_claude_agents_dir(registry, monkeypatch, tmp_path):
    monkeypatch.delenv("ATLAS_CLAUDE_AGENTS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.claude/agents → empty temp home
    loader = AgentLoader(registry, MODELS)
    r = loader.resolve("eos-traction")
    assert not r.available and ".claude/agents/eos-traction.md" in (r.reason or "").replace("\\", "/")
    assert not any(x.id.startswith("eos-") for x in loader.roster("corporate"))
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "eos-traction.md").write_text("---\nname: eos-traction\n---\nRocks.")
    assert AgentLoader(registry, MODELS).resolve("eos-traction").available


# ---------------------------------------------------------------------------
# node isolation
# ---------------------------------------------------------------------------


def test_node_context_isolation(two_nodes, local_ctx):
    ctx = NodeContext(two_nodes, local_dir=local_ctx)
    sofia, eos, atlas = (two_nodes.get(a) for a in ("sofia", "eos-data", "atlas"))
    personal = ctx.load("personal", sofia)
    assert "PERSONAL-SECRET" in personal and "PERSONAL-TRIP" in personal
    assert "CORP-SECRET" not in personal and "EOS-SECRET" not in personal  # symlink escape ignored
    corp = ctx.load("corporate", sofia)
    assert "CORP-SECRET" in corp and "EOS-SECRET" not in corp and "PERSONAL" not in corp
    assert "not text" not in corp
    assert "EOS-SECRET" in ctx.load("corporate", eos) and "CORP-SECRET" in ctx.load("corporate", eos)
    assert "EOS-SECRET" in ctx.load("corporate", atlas)
    assert ctx.load("mars", sofia) == "" and ctx.load("../personal", sofia) == ""
    assert sorted(ctx.nodes_with_context()) == ["corporate", "personal"]
    small = NodeContext(two_nodes, local_dir=local_ctx, max_chars=1000)
    (local_ctx / "context" / "personal" / "big.md").write_text("x" * 5000)
    assert len(small.load("personal", sofia)) == 1000


def _isolation_llm() -> FakeLLM:
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "oracle", "Analysis B")))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Analysis B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())
    return llm


def test_mission_node_isolation(two_nodes, local_ctx):
    personal_llm, corporate_llm = _isolation_llm(), _isolation_llm()

    async def go(llm, node):
        store, engine = make_live(two_nodes, llm, local=local_ctx)
        m = await engine.start("Plan", node)
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    asyncio.run(go(personal_llm, "personal"))
    asyncio.run(go(corporate_llm, "corporate"))

    personal_text = "\n".join(call_text(c) for c in personal_llm.calls)
    assert "PERSONAL-SECRET" in personal_text
    assert "CORP-SECRET" not in personal_text and "EOS-SECRET" not in personal_text
    roster = calls_matching(personal_llm, stage("PLANNING"))[0]["tools"][0]["input_schema"]["properties"][
        "tasks"]["items"]["properties"]["assigned_to"]["enum"]
    assert "sofia" in roster and not any(a.startswith("eos-") for a in roster) and "atlas" not in roster

    corp_text = "\n".join(call_text(c) for c in corporate_llm.calls)
    assert "CORP-SECRET" in corp_text and "PERSONAL" not in corp_text
    corp_roster = calls_matching(corporate_llm, stage("PLANNING"))[0]["tools"][0]["input_schema"][
        "properties"]["tasks"]["items"]["properties"]["assigned_to"]["enum"]
    assert "eos-data" in corp_roster  # claude_md file present → available in its own node
    # a non-EOS agent working a corporate task does not see the EOS division folder
    assert "EOS-SECRET" not in call_text(calls_matching(corporate_llm, task_call("Research A"))[0])
    assert "EOS-SECRET" in call_text(calls_matching(corporate_llm, stage("PLANNING"))[0])  # ATLAS does


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

SIM_SCENARIO = {
    "id": "live-test-pause",
    "node": "corporate",
    "title": "Pauses on approval",
    "objective": "Wait for a human.",
    "steps": [
        {"do": "phase", "phase": "DECOMPOSITION"},
        {"do": "task", "ref": "t1", "title": "Draft", "description": "d", "assigned_to": "alfred"},
        {"do": "agent", "agent": "alfred", "status": "WORKING", "task": "t1", "activity": "Drafting"},
        {"do": "approval", "ref": "a", "requested_by": "alfred", "reason": "EXTERNAL_COMMUNICATION",
         "title": "Send", "detail": "d", "task": "t1"},
        {"do": "phase", "phase": "CLOSED"},
    ],
}


def _poll(client: TestClient, pred, timeout: float = 3.0) -> WorldState:
    deadline = time.monotonic() + timeout
    while True:
        state = WorldState.model_validate(client.get("/state").json())
        if pred(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_http_modes_config_and_cancel(monkeypatch):
    from atlas.main import app
    from atlas.sim.scenario import Scenario

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        app.state.sim.library.directory = None
        app.state.sim.library.add(Scenario.model_validate(SIM_SCENARIO))

        cfg = client.get("/config").json()
        assert cfg["live_available"] is False and cfg["web_search"] is False
        assert set(cfg["models"]) >= {"orchestrator", "default"} and isinstance(cfg["context_nodes"], list)
        avail = client.get("/agents/availability").json()
        assert len(avail) == len(client.get("/agents").json()) and avail["sofia"] == {"available": True}

        r = client.post("/missions", json={"objective": "x", "node": "corporate", "mode": "live"})
        assert r.status_code == 422 and "ANTHROPIC_API_KEY" in r.json()["detail"]

        # no key, no mode -> simulated; cancel it while it waits for approval
        r = client.post("/missions", json={"objective": "Sim", "node": "corporate", "speed": 1000})
        sim_mission = Mission.model_validate(r.json())
        assert r.status_code == 200 and sim_mission.mode == "simulated"
        _poll(client, lambda s: any(a.state == "PENDING" for a in s.approvals))
        r = client.post(f"/missions/{sim_mission.id}/cancel")
        assert r.status_code == 200 and r.json()["phase"] == "CLOSED"
        state = WorldState.model_validate(client.get("/state").json())
        assert state.tasks[0].status == "CANCELLED" and state.approvals[0].state == "EXPIRED"
        assert {s.agent_id: s.status for s in state.agent_states}["alfred"] == "IDLE"
        assert client.post(f"/missions/{sim_mission.id}/cancel").status_code == 409
        assert client.post("/missions/msn_nope/cancel").status_code == 404

        # with a key: default mode is live (unless a scenario is given)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        app.state.live.llm = FakeLLM()  # no scripted response: planning fails, mission closes
        assert client.get("/config").json()["live_available"] is True
        r = client.post("/missions", json={"objective": "Live", "node": "corporate"})
        assert r.status_code == 200 and r.json()["mode"] == "live"
        live_id = r.json()["id"]
        state = _poll(client, lambda s: next(m for m in s.missions if m.id == live_id).phase == "CLOSED")
        assert next(x for x in state.mission_reports if x.mission_id == live_id).objective_status == "NOT_ACHIEVED"
        r = client.post("/missions", json={"objective": "x", "node": "corporate", "mode": "live",
                                           "scenario_id": "live-test-pause"})
        assert r.status_code == 422
        r = client.post("/missions", json={"objective": "Sim2", "node": "corporate",
                                           "scenario_id": "live-test-pause", "speed": 1000})
        assert r.status_code == 200 and r.json()["mode"] == "simulated"
        assert client.post("/reset").status_code == 200


def test_anthropic_llm_retry_and_sdk_blocks():
    """AnthropicLLM against the real SDK (mocked HTTP): one retry on overload, SDK blocks echo back."""
    import httpx2 as httpx

    from atlas.live import AnthropicLLM
    from atlas.live.llm import block_to_param, response_text, tool_uses

    body = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7,
                  "cache_creation_input_tokens": 0},
        "content": [
            {"type": "text", "text": "Checking.", "citations": None},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "rent"}},
            {"type": "tool_use", "id": "toolu_1", "name": "submit_report", "input": {"findings": []}},
        ],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error",
                                                                         "message": "Overloaded"}})
        return httpx.Response(200, json=body)

    async def go():
        llm = AnthropicLLM("sk-test", backoff=0.0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        msg = await llm.create(model="claude-sonnet-5", max_tokens=10, messages=[{"role": "user", "content": "x"}],
                               system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}])
        assert len(seen) == 2 and msg.usage.cache_read_input_tokens == 7
        assert response_text(msg) == "Checking." and tool_uses(msg)[0].name == "submit_report"
        params = [block_to_param(b) for b in msg.content]
        assert params[1]["type"] == "server_tool_use" and params[1]["input"] == {"query": "rent"}
        assert params[2] == {"type": "tool_use", "id": "toolu_1", "name": "submit_report", "input": {"findings": []}}

        seen.clear()
        failing = AnthropicLLM("sk-test", backoff=0.0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}))))
        with pytest.raises(LLMError, match="BadRequest"):
            await failing.create(model="m", max_tokens=1, messages=[])

    asyncio.run(go())
