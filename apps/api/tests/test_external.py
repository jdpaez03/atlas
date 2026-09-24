"""External agents in live missions (http / cli adapters, contract atlas.external/1) — no network."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path

import httpx
import pytest
from test_live import (
    MODELS,
    NO_FOLLOWUPS,
    make_live,
    mission_report,
    plan,
    report,
    stage,
    t,
    task_call,
    until,
)

from atlas.core.models import AgentDefinition
from atlas.core.registry import DEFAULT_AGENTS_DIR, AgentRegistry
from atlas.live import AgentLoader, FakeLLM
from atlas.live import external as ext
from atlas.live.external import SELF_REPORTED, command_argv, parse_response

ECHO = DEFAULT_AGENTS_DIR.parent / "examples" / "external-agents" / "echo_agent.py"


def _yaml(agent_id: str, adapter: str, config: dict) -> str:
    return json.dumps({  # JSON is valid YAML
        "id": agent_id, "name": agent_id.upper(), "title": "External", "description": f"External {agent_id}",
        "kind": "external", "adapter": adapter, "adapter_config": config, "capabilities": ["x"],
        "nodes": ["*"], "enabled": True,
    })


def _cmd(*parts: str) -> str:
    return " ".join(shlex.quote(p) for p in parts)


@pytest.fixture
def ext_registry(tmp_path: Path, monkeypatch) -> AgentRegistry:
    """The default agents plus: hook (http), echo (cli example), sleepy (cli, times out), crash (cli, exit 3),
    mcpy (mcp, unavailable)."""
    agents = tmp_path / "agents"
    shutil.copytree(DEFAULT_AGENTS_DIR, agents)
    sleepy = tmp_path / "sleepy.py"
    sleepy.write_text("import time\ntime.sleep(30)\n")
    crash = tmp_path / "crash.py"
    crash.write_text("import sys\nsys.stderr.write('boom: bad input')\nsys.exit(3)\n")
    monkeypatch.setenv("ATLAS_TEST_TOKEN", "s3cret")
    (agents / "hook.yaml").write_text(_yaml("hook", "http", {
        "url": "https://agent.example.test/run", "headers": {"Authorization": "Bearer ${ATLAS_TEST_TOKEN}"},
        "timeout_seconds": 5}))
    (agents / "echo.yaml").write_text(_yaml("echo", "cli", {"command": _cmd(sys.executable, str(ECHO))}))
    (agents / "sleepy.yaml").write_text(_yaml("sleepy", "cli", {
        "command": _cmd(sys.executable, str(sleepy)), "timeout_seconds": 0.5}))
    (agents / "crash.yaml").write_text(_yaml("crash", "cli", {"command": _cmd(sys.executable, str(crash))}))
    (agents / "mcpy.yaml").write_text(_yaml("mcpy", "mcp", {"server": "x"}))
    return AgentRegistry.load(agents)


@pytest.fixture
def transport(monkeypatch):
    """Installs an httpx.MockTransport; `set(handler)` changes the response; `requests` records the calls."""

    class T:
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.handler = lambda req: httpx.Response(200, json={})

        def set(self, handler):
            self.handler = handler

    box = T()

    def handle(req: httpx.Request) -> httpx.Response:
        box.requests.append(req)
        return box.handler(req)

    monkeypatch.setattr(ext, "_transport", httpx.MockTransport(handle))
    return box


def one_task_llm(agent: str, title: str = "External work", **kw) -> FakeLLM:
    """A plan with the external task plus one native task (plans need at least two)."""
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("x", agent, title, **kw), t("side", "sofia", "Side research")))
    llm.when(task_call("Side research"), report("Side fact"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())
    return llm


def run_mission(registry: AgentRegistry, llm: FakeLLM, objective: str = "Test external agents", on_start=None):
    async def go():
        store, engine = make_live(registry, llm)
        mission = await engine.start(objective, "corporate")
        if on_start:
            await on_start(store)
        await asyncio.wait_for(engine.wait(mission.id), 15)
        return store, mission.id

    store, mid = asyncio.run(go())
    return store, store.snapshot(), mid


def report_of(snap, agent_id: str):
    return next(r for r in snap.agent_reports if r.agent_id == agent_id)


# ---------------------------------------------------------------------------
# loader + helpers
# ---------------------------------------------------------------------------


def test_loader_availability(ext_registry, monkeypatch):
    loader = AgentLoader(ext_registry, MODELS)
    for aid in ("hook", "echo"):
        r = loader.resolve(aid)
        assert r.available and r.source == "generic" and r.agent.name in r.role_prompt
    mcp = loader.resolve("mcpy")
    assert not mcp.available and "not supported yet" in (mcp.reason or "")
    assert {r.id for r in loader.roster("corporate")} >= {"hook", "echo", "sofia"}

    def agent(adapter: str, config: dict) -> AgentDefinition:
        return AgentDefinition(id="z", name="Z", title="z", description="z", kind="external", adapter=adapter,
                               adapter_config=config)

    bad = [agent("http", {"url": "ftp://x"}), agent("http", {"url": "${ATLAS_UNSET_URL_X}/run"}),
           agent("cli", {"command": "  "})]
    for a in bad:
        loader.registry = type("R", (), {"get": staticmethod(lambda _id, a=a: a)})()
        assert not loader.resolve("z").available
    monkeypatch.setenv("ATLAS_UNSET_URL_X", "https://ok.test")
    good = agent("http", {"url": "${ATLAS_UNSET_URL_X}/run"})
    loader.registry = type("R", (), {"get": staticmethod(lambda _id: good)})()
    assert loader.resolve("z").available


def test_example_yaml_is_ignored_but_valid():
    reg = AgentRegistry.load()
    assert "echo" not in {a.id for a in reg.all(include_disabled=True)}
    import yaml

    data = yaml.safe_load((DEFAULT_AGENTS_DIR / "_example.cli.yaml").read_text())
    agent = AgentDefinition.model_validate(data)
    argv = command_argv(agent.adapter_config["command"])
    assert argv[0] == sys.executable and Path(argv[1]) == ECHO


def test_command_argv_and_parse(monkeypatch):
    monkeypatch.setenv("ATLAS_TEST_DIR", "/tmp/with space")
    assert command_argv("python '${ATLAS_TEST_DIR}/a.py' --x 1") == ["python", "/tmp/with space/a.py", "--x", "1"]
    assert command_argv("python ${ATLAS_TEST_DIR}/a.py") == ["python", "/tmp/with space/a.py"]
    with pytest.raises(ext.ExternalAgentError, match="unresolved"):
        command_argv("python ${ATLAS_NOT_SET_ANYWHERE}/a.py")
    assert parse_response(b'{"report": {"confidence": "LOW"}}') == {"confidence": "LOW"}
    assert parse_response(b'log line\n{"findings": []}\n') == {"findings": []}
    assert parse_response(b"not json") is None and parse_response(b"[1, 2]") is None


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------


def test_http_agent_in_live_mission(ext_registry, transport):
    transport.set(lambda req: httpx.Response(200, json={"report": {
        "actions_taken": ["Queried the CRM"],
        "findings": [
            {"kind": "FACT", "statement": "12 open deals"},  # FACT without sources -> confidence capped
            {"kind": "GUESS", "statement": "Pipeline looks healthy", "confidence": "VERY"},
            "A bare string finding",
        ],
        "confidence": "HIGH",
    }}))
    _store, snap, mid = run_mission(ext_registry, one_task_llm("hook", "Count CRM deals"))
    task = next(x for x in snap.tasks if x.assigned_to == "hook")
    assert task.status == "COMPLETED"
    (req,) = transport.requests
    assert req.method == "POST" and str(req.url) == "https://agent.example.test/run"
    assert req.headers["authorization"] == "Bearer s3cret"
    body = json.loads(req.content)
    assert body["contract"] == "atlas.external/1"
    assert body["mission"] == {"id": mid, "objective": "Test external agents", "node": "corporate"}
    assert body["task"]["id"] == task.id and body["task"]["title"] == "Count CRM deals"
    assert body["task"]["priority"] == "MEDIUM" and body["task"]["requires_approval"] is False
    assert body["agent"] == {"id": "hook", "name": "HOOK"} and body["inputs"] == []
    assert body["deadline_seconds"] == 5

    rep = report_of(snap, "hook")
    assert rep.confidence == "MEDIUM"  # HIGH capped: a FACT has no sources
    assert [c.kind for c in rep.findings] == ["FACT", "ASSUMPTION", "ASSUMPTION"]
    assert rep.findings[1].confidence == "MEDIUM"
    assert SELF_REPORTED in rep.limitations
    (call,) = [e for e in rep.evidence if e.kind == "external_call"]
    assert call.ok and call.ref == "https://agent.example.test/run" and call.detail.startswith("HTTP 200")
    assert "s3cret" not in json.dumps(snap.model_dump(mode="json"))  # the secret never reaches the state
    assert next(m for m in snap.missions if m.id == mid).phase == "CLOSED"


def test_http_500_fails_the_task(ext_registry, transport):
    transport.set(lambda req: httpx.Response(500, text="internal kaboom"))
    _store, snap, mid = run_mission(ext_registry, one_task_llm("hook"))
    task = next(x for x in snap.tasks if x.assigned_to == "hook")
    assert task.status == "FAILED"
    (call,) = [e for e in snap.evidence if e.kind == "external_call"]
    assert not call.ok and "500" in call.detail and "kaboom" in call.detail
    assert not [r for r in snap.agent_reports if r.agent_id == "hook"]
    assert next(m for m in snap.missions if m.id == mid).phase == "CLOSED"


def test_http_non_json_is_wrapped_low(ext_registry, transport):
    transport.set(lambda req: httpx.Response(200, text="Sure! I did the thing."))
    _store, snap, _ = run_mission(ext_registry, one_task_llm("hook"))
    rep = report_of(snap, "hook")
    assert rep.confidence == "LOW" and "Sure! I did the thing." in rep.findings[0].statement
    assert rep.findings[0].kind == "ASSUMPTION"
    assert any("Auto-wrapped" in x for x in rep.limitations) and SELF_REPORTED in rep.limitations
    assert [e.kind for e in rep.evidence] == ["external_call"]


def test_approval_rejected_skips_the_call(ext_registry, transport):
    async def reject(store):
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        apr = store.snapshot().approvals[0]
        assert apr.requested_by == "hook" and "external http agent" in (apr.proposed_action or "")
        assert next(s for s in store.snapshot().agent_states if s.agent_id == "hook").status == "WAITING"
        await store.decide_approval(apr.id, "REJECTED", note="no")

    llm = one_task_llm("hook", requires_approval=True, approval_reason="EXTERNAL_COMMUNICATION")
    _store, snap, _ = run_mission(ext_registry, llm, on_start=reject)
    assert transport.requests == []
    rep = report_of(snap, "hook")
    assert "not executed" in rep.findings[0].statement and rep.confidence == "LOW"
    assert [e.kind for e in rep.evidence] == ["approval"]


def test_approval_approved_then_called(ext_registry, transport):
    transport.set(lambda req: httpx.Response(200, json={"findings": [
        {"kind": "FACT", "statement": "Sent", "sources": ["outbox"]}], "confidence": "HIGH"}))

    async def approve(store):
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        assert transport.requests == []  # nothing is called before the decision
        await store.decide_approval(store.snapshot().approvals[0].id, "APPROVED")

    llm = one_task_llm("hook", requires_approval=True)
    _store, snap, _ = run_mission(ext_registry, llm, on_start=approve)
    assert len(transport.requests) == 1
    rep = report_of(snap, "hook")
    assert rep.confidence == "HIGH"  # every FACT has sources
    assert [e.kind for e in rep.evidence] == ["approval", "external_call"]


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def test_cli_echo_after_native_agent(ext_registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(
        t("research", "sofia", "Market research"),
        t("count", "echo", "Count words", ["research"]),
    ))
    llm.when(task_call("Market research"), report("Average rent is 18k MXN"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())
    _store, snap, mid = run_mission(ext_registry, llm)
    assert {x.status for x in snap.tasks} == {"COMPLETED"}
    assert next(m for m in snap.missions if m.id == mid).phase == "CLOSED"
    rep = report_of(snap, "echo")
    assert rep.asked_to == "Count words" and rep.confidence == "HIGH"
    fact = rep.findings[0]
    assert fact.kind == "FACT" and "task description has 3 words" in fact.statement
    assert "1 dependency input(s)" in fact.statement and rep.inputs_used == ["task.description", "inputs[0]"]
    assert rep.findings[1].kind == "ASSUMPTION"
    assert SELF_REPORTED in rep.limitations
    (call,) = [e for e in rep.evidence if e.kind == "external_call"]
    assert call.ok and call.detail.startswith("exit 0") and "echo_agent.py" in call.ref


def test_cli_timeout_kills_and_fails(ext_registry):
    _store, snap, _ = run_mission(ext_registry, one_task_llm("sleepy"))
    task = next(x for x in snap.tasks if x.assigned_to == "sleepy")
    assert task.status == "FAILED"
    (call,) = [e for e in snap.evidence if e.kind == "external_call"]
    assert not call.ok and "timed out" in call.detail


def test_cli_nonzero_exit_fails_with_stderr(ext_registry):
    _store, snap, _ = run_mission(ext_registry, one_task_llm("crash"))
    assert next(x for x in snap.tasks if x.assigned_to == "crash").status == "FAILED"
    (call,) = [e for e in snap.evidence if e.kind == "external_call"]
    assert not call.ok and "code 3" in call.detail and "boom: bad input" in call.detail


def test_cli_cancellation_kills_child(tmp_path):
    marker = tmp_path / "alive"
    script = tmp_path / "slow.py"
    script.write_text(f"import time, pathlib\ntime.sleep(1.5)\npathlib.Path({str(marker)!r}).write_text('x')\n")

    async def go():
        job = asyncio.create_task(ext.call_cli({"command": _cmd(sys.executable, str(script))}, b"{}", 30))
        await asyncio.sleep(0.3)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        await asyncio.sleep(2)

    asyncio.run(go())
    assert not marker.exists()
