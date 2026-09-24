"""Subscription backend (Claude Agent SDK → Claude Code) — FakeClaudeSDK at the SDK boundary, no CLI."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core.events import EventBus
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.live import AgentLoader, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live import backend as backend_mod
from atlas.live.backend import detect_backend
from atlas.live.sdk import (
    BUILTIN_TOOLS,
    FakeClaudeSDK,
    FakeSession,
    SubscriptionExecutor,
    api_error,
    builtin,
    call,
    say,
)

MODELS = ModelConfig(orchestrator="opus", default="sonnet", fast="haiku")
FILE_AND_SHELL = {"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "Task", "Agent",
                  "TodoWrite", "Skill", "PowerShell"}


def stage(name: str):
    return lambda s: f"STAGE: {name}" in s.prompt


def task_call(title: str):
    return lambda s: f"Your task: {title}" in s.prompt


def is_consult(s: FakeSession) -> bool:
    return "(consultation)" in s.system


def t(ref: str, agent: str, title: str, deps: list[str] | None = None, **kw) -> dict:
    return {"ref": ref, "title": title, "description": f"Do {title.lower()}", "assigned_to": agent,
            "depends_on": deps or [], **kw}


def plan(*tasks: dict) -> list:
    return [call("create_plan", {"rationale": "split the work", "tasks": list(tasks)}), say("done")]


def report(statement: str, kind: str = "FACT") -> tuple:
    return call("submit_report", {
        "asked_to": "the task", "actions_taken": ["worked"], "inputs_used": ["context"],
        "findings": [{"kind": kind, "statement": statement, "sources": ["ctx"], "confidence": "HIGH"}],
        "confidence": "HIGH",
    })


NO_FOLLOWUPS = [call("request_followups", {"assessment": "complete", "tasks": []}), say("done")]
MISSION_REPORT = [call("submit_mission_report", {
    "executive_summary": "All good.", "objective_status": "ACHIEVED",
    "key_findings": [{"kind": "RECOMMENDATION", "statement": "Proceed", "confidence": "MEDIUM"}],
}), say("done")]


def make_sub(registry: AgentRegistry, sdk: FakeClaudeSDK, **cfg) -> tuple[WorldStore, LiveEngine]:
    store = WorldStore(registry, EventBus())
    config = LiveConfig(models=MODELS, **({"max_turns": 8, "web_search": True} | cfg))
    engine = LiveEngine(
        store, loader=AgentLoader(registry, MODELS),
        context=NodeContext(registry, local_dir=Path("/nonexistent-atlas-local")),
        config=config, prices=PriceTable(), backend="subscription", sdk_query=sdk,
    )
    return store, engine


async def until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.001)


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


# ---------------------------------------------------------------------------
# full mission
# ---------------------------------------------------------------------------


def test_full_subscription_mission(registry):
    sdk = FakeClaudeSDK(cost=0.02)
    sdk.when(is_consult, [say("Use a 10% discount rate (ASSUMPTION).")])
    sdk.when(stage("PLANNING"), [
        call("create_plan", {"tasks": [t("a", "sofia", "A", ["b"]), t("b", "oracle", "B", ["a"])]}),  # cycle
        call("create_plan", {"rationale": "split", "tasks": [
            t("research", "sofia", "Market research", priority="HIGH"),
            t("check", "argos", "Consistency check"),
            t("analysis", "oracle", "Scenario analysis", ["research", "check"], requires_approval=True,
              approval_reason="FINANCIAL_COMMITMENT"),
        ]}),
        say("done"),
    ])
    sdk.when(task_call("Market research"), [
        builtin("WebSearch", {"query": "Polanco rents 2026"}),
        call("consult", {"agent_id": "oracle", "question": "Which discount rate should I use?"}),
        report("Average rent is 18k MXN"), say("done"),
    ])
    sdk.when(task_call("Consistency check"), [report("Pro-forma matches data room", kind="ASSUMPTION"), say("ok")])
    sdk.when(task_call("Scenario analysis"), [
        report("too early"),  # refused: approval first
        call("request_approval", {"reason": "FINANCIAL_COMMITMENT", "title": "Commit deposit",
                                  "detail": "Reserve the unit", "proposed_action": "Pay 50k"}),
        report("Base case IRR 14%", kind="SCENARIO"), say("done"),
    ])
    sdk.when(stage("REVIEW"), NO_FOLLOWUPS)
    sdk.when(stage("CONSOLIDATION"), MISSION_REPORT)

    async def go():
        store, engine = make_sub(registry, sdk)
        initial = store.snapshot()
        mission = await engine.start("Evaluate the Polanco 2BR", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        snap = store.snapshot()
        assert next(x for x in snap.tasks if x.title == "Scenario analysis").status == "AWAITING_APPROVAL"
        assert next(s for s in snap.agent_states if s.agent_id == "oracle").status == "WAITING"
        await store.decide_approval(snap.approvals[0].id, "APPROVED", note="go ahead")
        await asyncio.wait_for(engine.wait(mission.id), 3)
        return store, engine, initial, mission.id

    store, engine, initial, mid = asyncio.run(go())
    snap = store.snapshot()
    mission = next(m for m in snap.missions if m.id == mid)
    assert mission.phase == "CLOSED" and mission.mode == "live" and engine.running() == []
    tasks = {x.title: x for x in snap.tasks}
    assert {x.status for x in tasks.values()} == {"COMPLETED"} and len(snap.agent_reports) == 3
    assert tasks["Scenario analysis"].depends_on == [tasks["Market research"].id, tasks["Consistency check"].id]

    events = store.bus.history()
    phases = [e.payload["mission"]["phase"] for e in events if e.type in ("mission.phase_changed", "mission.closed")]
    assert phases == ["DECOMPOSITION", "DELEGATION", "EXECUTION", "COLLABORATION", "VALIDATION",
                      "CONSOLIDATION", "REPORTING", "FOLLOW_UP", "CLOSED"]
    # the two independent tasks ran in parallel
    upd = [e for e in events if e.type == "task.updated"]
    started = [i for i, e in enumerate(upd) if e.payload["task"]["status"] == "IN_PROGRESS"
               and e.payload["task"]["title"] in ("Market research", "Consistency check")]
    first_done = next(i for i, e in enumerate(upd) if e.payload["task"]["status"] == "COMPLETED")
    assert len(started) >= 2 and max(started[:2]) < first_done

    # plan: invalid input went back to ATLAS as an error tool result, fixed in the same session
    plan_session = sdk.matching(stage("PLANNING"))[0]
    assert len(sdk.matching(stage("PLANNING"))) == 1
    assert plan_session.results[0][2] and "cycle" in plan_session.results[0][1]
    assert not plan_session.results[1][2]
    assert any("plan rejected (retrying)" in e.summary for e in events)
    assert plan_session.options.model == "opus" and "# ATLAS orchestrator protocol" in plan_session.system

    # consult: nested one-shot session on the fast model with the target's role prompt, REQUEST + ANSWER
    consult = sdk.matching(is_consult)[0]
    assert consult.options.model == "haiku" and consult.options.tools == [] and consult.options.mcp_servers == {}
    assert consult.options.max_turns == 1
    assert [(m.from_agent, m.to_agent, m.type) for m in snap.messages] == [
        ("sofia", "oracle", "REQUEST"), ("oracle", "sofia", "ANSWER")]
    research = sdk.matching(task_call("Market research"))[0]
    assert research.results[0] == ("consult", "Use a 10% discount rate (ASSUMPTION).", False)
    assert any("SOFIA searched the web · Polanco rents 2026" in e.summary for e in events)

    # approval gate, then the decision + note come back as the tool result
    oracle = sdk.matching(task_call("Scenario analysis"))[0]
    assert oracle.results[0][2] and "request_approval first" in oracle.results[0][1]
    assert "APPROVED" in oracle.results[1][1] and "go ahead" in oracle.results[1][1]
    assert "Average rent is 18k MXN" in oracle.prompt and "Pro-forma matches data room" in oracle.prompt

    # web tools only for research agents; everyone else has no built-in tool at all
    assert research.options.tools == ["WebSearch", "WebFetch"]
    assert sdk.matching(task_call("Consistency check"))[0].options.tools == []
    assert oracle.options.tools == []
    assert research.options.model == "sonnet"

    # usage: one record per session (7 sessions), API-equivalent cost from the SDK
    assert len(sdk.sessions) == 7
    assert len([e for e in events if e.type == "mission.updated"]) == 7
    assert mission.usage.input_tokens == 700 and mission.usage.output_tokens == 350
    assert mission.usage.est_cost_usd == pytest.approx(0.14)
    final = snap.mission_reports[0]
    assert final.objective_status == "ACHIEVED" and mission.final_report_id == final.id

    states = {s.agent_id: s for s in snap.agent_states}
    assert states["argos"].status == "MONITORING"
    for a in ("atlas", "sofia", "oracle"):
        assert states[a].status == "IDLE"
    assert fold(initial, store.bus.history()).model_dump(mode="json") == snap.model_dump(mode="json")


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


def test_sdk_options_are_locked_down(registry, tmp_path):
    ex = SubscriptionExecutor(query=FakeClaudeSDK())
    tool = ex.tool("submit_report", "d", {"type": "object", "properties": {}}, lambda a: None)
    for web in (False, True):
        opts = ex.options(model="sonnet", system_file=str(tmp_path / "p.md"), tools=[tool], web=web, max_turns=8)
        allowed_builtins = {"WebSearch", "WebFetch"} if web else set()
        assert set(opts.tools) == allowed_builtins  # nothing else is even loaded
        assert not FILE_AND_SHELL & set(opts.tools) and not FILE_AND_SHELL & set(opts.allowed_tools)
        assert FILE_AND_SHELL <= set(opts.disallowed_tools)
        assert set(opts.allowed_tools) == {"mcp__atlas__submit_report"} | allowed_builtins
        assert not allowed_builtins & set(opts.disallowed_tools)
        assert opts.setting_sources == [] and opts.skills == [] and opts.strict_mcp_config is True
        assert opts.permission_mode == "dontAsk" and opts.verbatim_prompts is True
        assert list(opts.mcp_servers) == ["atlas"] and opts.mcp_servers["atlas"]["type"] == "sdk"
        cwd = Path(opts.cwd)
        assert cwd.is_dir() and list(cwd.iterdir()) == [] and cwd != Path.cwd()
        assert opts.env["ANTHROPIC_API_KEY"] == ""  # bills the plan, not an API key from .env
        assert opts.system_prompt == {"type": "file", "path": str(tmp_path / "p.md")}
        assert opts.max_turns == 8 and opts.agents is None and opts.plugins == []
        assert "no-chrome" in opts.extra_args and "mcp__claude-in-chrome" in opts.disallowed_tools
    assert {"Bash", "Read", "Write", "Edit"} <= set(BUILTIN_TOOLS)

    # the CLI command line the SDK would build (no CLI is started)
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    opts = ex.options(model="sonnet", system_file="/x/p.md", tools=[tool], web=False, max_turns=8)
    tr = SubprocessCLITransport(prompt="x", options=opts)
    tr._cli_path = "claude"
    cmd = tr._build_command()
    assert cmd[cmd.index("--tools") + 1] == "" and "--setting-sources=" in cmd and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__atlas__submit_report"
    assert cmd[cmd.index("--system-prompt-file") + 1] == "/x/p.md"
    assert "Bash" in cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "--no-chrome" in cmd and "--no-session-persistence" in cmd


def test_every_session_is_locked_down(registry):
    """Across a whole mission, no session gets a file/shell tool or the user's settings."""
    sdk = FakeClaudeSDK()
    sdk.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "alfred", "Draft B")))
    sdk.when(task_call("Research A"), [report("A")])
    sdk.when(task_call("Draft B"), [builtin("Bash", {"command": "rm -rf /"}), report("B")])
    sdk.when(stage("REVIEW"), NO_FOLLOWUPS)
    sdk.when(stage("CONSOLIDATION"), MISSION_REPORT)

    async def go():
        store, engine = make_sub(registry, sdk)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    snap = asyncio.run(go()).snapshot()
    assert snap.missions[0].phase == "CLOSED"
    for s in sdk.sessions:
        assert not FILE_AND_SHELL & set(s.options.tools) and not FILE_AND_SHELL & set(s.options.allowed_tools)
        assert s.options.setting_sources == [] and s.options.permission_mode == "dontAsk"
    bash = sdk.matching(task_call("Draft B"))[0]
    assert bash.results[0] == ("Bash", "permission denied", True)


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


def test_usage_limit_fails_task_and_mission_closes(registry):
    sdk = FakeClaudeSDK()
    sdk.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B"),
                                     t("c", "oracle", "Analysis C", ["a"]), t("d", "alfred", "Draft D", ["b"])))
    sdk.when(task_call("Research A"), [
        api_error("rate_limit", "Claude AI usage limit reached|1790000000",
                  rate={"status": "rejected", "resets_at": 1790000000, "rate_limit_type": "five_hour"}, status=429),
    ])
    sdk.when(task_call("Check B"), [report("B")])

    async def go():
        store, engine = make_sub(registry, sdk, max_concurrency=1)  # A fails first, then B runs, D never starts
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    store = asyncio.run(go())
    snap = store.snapshot()
    status = {x.title: x.status for x in snap.tasks}
    assert status["Research A"] == "FAILED" and status["Analysis C"] == "CANCELLED"
    assert status["Draft D"] in ("FAILED", "CANCELLED")
    assert snap.missions[0].phase == "CLOSED"
    final = snap.mission_reports[0]
    assert any("Max plan usage limit reached (five-hour) — resets at" in x for x in final.needs_human_attention)
    assert "Automatic consolidation" in final.executive_summary  # no more LLM calls after the limit
    summaries = [e.summary for e in store.bus.history()]
    assert any("Max plan usage limit reached" in x and "stops starting new work" in x for x in summaries)
    assert any("Review skipped" in x for x in summaries)
    sofia = next(s for s in store.bus.history() if s.type == "agent.state_changed"
                 and s.payload["state"]["agent_id"] == "sofia" and s.payload["state"]["status"] == "ERROR")
    assert "usage limit" in sofia.payload["state"]["activity"]
    assert not sdk.matching(stage("REVIEW")) and not sdk.matching(stage("CONSOLIDATION"))
    assert snap.missions[0].usage.llm_calls >= 2  # the failed session's usage was still recorded


def test_login_error_and_max_turns_fallback(registry):
    sdk = FakeClaudeSDK()
    sdk.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B")))
    sdk.when(task_call("Research A"), [api_error("authentication_failed", "Invalid API key · Please run /login")])
    sdk.when(task_call("Check B"), [say("notes 1"), say("notes 2"), say("notes 3")])  # never submits
    sdk.when(stage("REVIEW"), NO_FOLLOWUPS)
    sdk.when(stage("CONSOLIDATION"), MISSION_REPORT)

    async def go():
        store, engine = make_sub(registry, sdk, max_turns=2)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 3)
        return store

    store = asyncio.run(go())
    snap = store.snapshot()
    assert {x.title: x.status for x in snap.tasks} == {"Research A": "FAILED", "Check B": "COMPLETED"}
    assert any("not logged in" in x for x in snap.mission_reports[0].needs_human_attention)
    wrapped = snap.agent_reports[0]
    assert wrapped.confidence == "LOW" and "notes 2" in wrapped.findings[0].statement


# ---------------------------------------------------------------------------
# backend selection + /config
# ---------------------------------------------------------------------------


def test_backend_auto_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("ATLAS_CLAUDE_CLI", raising=False)
    monkeypatch.setenv("ATLAS_LLM_BACKEND", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert detect_backend().backend == "api"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(backend_mod, "find_claude_cli", lambda: "/opt/claude")
    info = detect_backend()
    assert info.backend == "subscription" and info.cost_basis == "api_equivalent" and info.hint is None

    monkeypatch.setattr(backend_mod, "find_claude_cli", lambda: None)
    info = detect_backend()
    assert info.backend is None and "ANTHROPIC_API_KEY" in info.hint and "Claude Code CLI not found" in info.hint

    monkeypatch.setattr(backend_mod, "sdk_installed", lambda: False)
    assert "claude-agent-sdk is not installed" in detect_backend("subscription").hint

    monkeypatch.setattr(backend_mod, "sdk_installed", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(backend_mod, "find_claude_cli", lambda: "/opt/claude")
    assert detect_backend("subscription").backend == "subscription"  # explicit choice wins over the key
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert "ATLAS_LLM_BACKEND=subscription" in detect_backend("api").hint
    assert "Unknown ATLAS_LLM_BACKEND" in detect_backend("nope").hint

    # CLI discovery: explicit path, else the SDK's bundled CLI or PATH
    monkeypatch.undo()
    monkeypatch.setenv("ATLAS_CLAUDE_CLI", str(tmp_path / "missing.exe"))
    assert backend_mod.find_claude_cli() is None
    assert "ATLAS_CLAUDE_CLI" in detect_backend("subscription").hint
    (tmp_path / "claude.exe").write_text("")
    monkeypatch.setenv("ATLAS_CLAUDE_CLI", str(tmp_path / "claude.exe"))
    assert backend_mod.find_claude_cli() == str(tmp_path / "claude.exe")

    # model + web defaults per backend
    monkeypatch.delenv("ATLAS_MODEL", raising=False)
    monkeypatch.delenv("ATLAS_WEB_SEARCH", raising=False)
    sub, api = LiveConfig.from_env("subscription"), LiveConfig.from_env("api")
    assert (sub.models.orchestrator, sub.models.default, sub.models.fast) == ("opus", "sonnet", "haiku")
    assert sub.web_search is True and api.web_search is False and api.models.default == "claude-sonnet-5"
    monkeypatch.setenv("ATLAS_WEB_SEARCH", "0")
    monkeypatch.setenv("ATLAS_MODEL", "claude-sonnet-5")
    sub = LiveConfig.from_env("subscription")
    assert sub.web_search is False and sub.models.default == "claude-sonnet-5"


def test_http_config_and_live_on_subscription(monkeypatch):
    from atlas.main import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ATLAS_LLM_BACKEND", "subscription")
    monkeypatch.setattr(backend_mod, "find_claude_cli", lambda: None)
    with TestClient(app) as client:
        cfg = client.get("/config").json()
        assert cfg["live_available"] is False and cfg["backend"] is None and cfg["cost_basis"] is None
        assert "Claude Code CLI not found — install Claude Code and log in" in cfg["live_hint"]
        r = client.post("/missions", json={"objective": "x", "node": "corporate", "mode": "live"})
        assert r.status_code == 422 and "Claude Code CLI not found" in r.json()["detail"]

        monkeypatch.setattr(backend_mod, "find_claude_cli", lambda: "/opt/claude")
        sdk = FakeClaudeSDK()  # no scripted session: planning fails, the mission closes
        app.state.live._sdk_query = sdk
        app.state.live._executors.clear()
        cfg = client.get("/config").json()
        assert cfg["live_available"] is True and cfg["backend"] == "subscription"
        assert cfg["cost_basis"] == "api_equivalent" and cfg["live_hint"] is None
        assert cfg["models"] == {"orchestrator": "opus", "default": "sonnet", "fast": "haiku"}
        assert cfg["web_search"] is True
        r = client.post("/missions", json={"objective": "Live", "node": "corporate"})
        assert r.status_code == 200 and r.json()["mode"] == "live"
        deadline = time.monotonic() + 3
        while client.get("/state").json()["missions"][-1]["phase"] != "CLOSED":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(sdk.sessions) == 1 and sdk.sessions[0].options.model == "opus"

        monkeypatch.setenv("ATLAS_LLM_BACKEND", "api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        cfg = client.get("/config").json()
        assert cfg["backend"] == "api" and cfg["cost_basis"] == "api" and cfg["live_hint"] is None
        assert client.post("/reset").status_code == 200
