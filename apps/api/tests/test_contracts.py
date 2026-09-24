from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core.models import AgentMessage, AgentStatus, ApprovalRequest, Mission, Task
from atlas.core.registry import DEFAULT_AGENTS_DIR, AgentRegistry, RegistryError


def test_registry_loads_initial_team():
    reg = AgentRegistry.load()
    core = {"atlas", "sofia", "argos", "oracle", "alfred"}
    eos = {f"eos-{c}" for c in ["vision", "people", "data", "issues", "process", "traction"]}
    assert {a.id for a in reg.all()} == core | eos | {"market-studies", "hermes", "auditor"}
    assert not reg.get("auditor").plannable
    assert not reg.can_work_in("market-studies", "personal")
    assert not reg.can_work_in("hermes", "personal")
    assert reg.orchestrator.id == "atlas"
    assert {a.id for a in reg.division_members("eos")} == eos


def test_template_is_ignored_and_external_needs_adapter_config(tmp_path: Path):
    for f in DEFAULT_AGENTS_DIR.glob("*.yaml"):  # top level only: core agents + organization
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nname: BAD\ntitle: x\ndescription: x\nkind: external\nadapter: http\n"
    )
    with pytest.raises(RegistryError, match="adapter_config.url"):
        AgentRegistry.load(tmp_path)


def test_monitoring_status_exists():
    assert AgentStatus.MONITORING.value == "MONITORING"


def test_message_uses_from_to_aliases():
    m = AgentMessage.model_validate(
        {"mission_id": "m", "from": "sofia", "to": "oracle", "type": "RESULT",
         "subject": "Market analysis", "body": "done", "confidence": "HIGH"}
    )
    assert m.from_agent == "sofia"
    assert '"from":"sofia"' in m.model_dump_json(by_alias=True)


def test_task_and_approval_defaults():
    mission = Mission(objective="Analyze investment")
    task = Task(mission_id=mission.id, title="Research", description="x", assigned_to="sofia")
    assert task.status == "PENDING" and task.id.startswith("tsk_")
    apr = ApprovalRequest(mission_id=mission.id, requested_by="alfred",
                          reason="EXTERNAL_COMMUNICATION", title="Send email", detail="x")
    assert apr.state == "PENDING"


def test_api_health_agents_events():
    from atlas.main import app
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert len(client.get("/agents").json()) == 14
        events = client.get("/events").json()
        assert events and events[0]["summary"].startswith("ATLAS online")


def test_node_isolation():
    reg = AgentRegistry.load()
    assert reg.can_work_in("eos-traction", "corporate")
    assert not reg.can_work_in("eos-traction", "personal")
    assert reg.can_work_in("sofia", "personal")  # shared core agent
    personal = {a.id for a in reg.agents_for_node("personal")}
    assert not any(a.startswith("eos-") for a in personal)
    assert Mission(objective="x").node == "corporate"


def test_division_member_must_live_in_division_node(tmp_path: Path):
    for f in DEFAULT_AGENTS_DIR.glob("*.yaml"):
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / "leak.yaml").write_text(
        "id: leak\nname: LEAK\ntitle: x\ndescription: x\nnodes: ['*']\ndivision: eos\n"
    )
    with pytest.raises(RegistryError, match="members must have nodes"):
        AgentRegistry.load(tmp_path)


def test_env_paths_resolve(monkeypatch):
    from atlas.core.registry import resolve_path
    monkeypatch.setenv("ATLAS_CLAUDE_AGENTS_DIR", "/x/agents")
    assert resolve_path("${ATLAS_CLAUDE_AGENTS_DIR}/eos-data.md").as_posix() == "/x/agents/eos-data.md"
