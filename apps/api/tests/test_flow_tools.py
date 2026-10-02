"""The agents' Power Automate tools (atlas/live/flowtools.py): reads, and changes only after approval."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_live import (
    NO_FOLLOWUPS,
    calls_matching,
    make_live,
    mission_report,
    plan,
    report,
    stage,
    t,
    task_call,
    until,
)

from atlas.core.registry import AgentRegistry
from atlas.live import FakeLLM, flowtools
from atlas.live.llm import call_text, tool_use

ENV = "Default-1111aaaa-2222-bbbb-3333-cccc4444dddd"
FID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
BASE = f"/providers/Microsoft.ProcessSimple/environments/{ENV}/flows"
FLOW = {"name": FID, "properties": {
    "displayName": "Aviso cobranza vencida", "state": "Started", "lastModifiedTime": "2026-09-30T10:00:00Z",
    "definitionSummary": {"triggers": [{"type": "Request", "kind": "Button"}]},
    "definition": {"triggers": {"manual": {"type": "Request", "kind": "Button"}},
                   "actions": {"Send_an_email": {"type": "OpenApiConnection"}}},
    "connectionReferences": {"shared_office365": {"connectionName": "x"}}}}


@pytest.fixture
def api(monkeypatch):
    seen: list[tuple[str, str, dict, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, str(req.url), dict(req.headers), body))
        path = req.url.path
        if req.url.host == "api.flow.microsoft.com":
            assert req.url.params["api-version"] == "2016-11-01" and req.headers["Authorization"] == "Bearer flow-tok"
            if path.endswith("/environments"):
                return httpx.Response(200, json={"value": [
                    {"name": "other", "properties": {"displayName": "Dev", "isDefault": False}},
                    {"name": ENV, "properties": {"displayName": "PAGA (default)", "isDefault": True}}]})
            if path == BASE:
                return httpx.Response(200, json={"value": [FLOW]})
            if path == f"{BASE}/{FID}":
                return httpx.Response(200, json=FLOW)
            if path == f"{BASE}/{FID}/runs":
                return httpx.Response(200, json={"value": [{"name": "08585", "properties": {
                    "status": "Failed", "startTime": "2026-10-01T08:00:00Z", "error": {"message": "Unauthorized"}}}]})
            if path == f"{BASE}/{FID}/triggers/manual/listCallbackUrl":
                return httpx.Response(200, json={"response": {
                    "value": "https://prod-01.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"}})
            if path.endswith(("/stop", "/start")):
                return httpx.Response(200)
        if req.url.host == "prod-01.westus.logic.azure.com":
            assert "Authorization" not in req.headers  # a signed URL never also carries a token
            return httpx.Response(202)
        if req.url.host == "paga.crm.dynamics.com":
            assert req.headers["Authorization"] == "Bearer dv-tok" and req.headers["Prefer"] == "return=representation"
            return httpx.Response(201, json={"workflowid": "wf-123", "name": body["name"]})
        return httpx.Response(404, json={"error": path})

    monkeypatch.setenv("ATLAS_MS_CLIENT_ID", "app")
    monkeypatch.setenv("ATLAS_FLOW_ENABLED", "1")
    monkeypatch.setenv("ATLAS_FLOW_WRITE", "1")
    monkeypatch.setenv("ATLAS_FLOW_DATAVERSE_URL", "https://paga.crm.dynamics.com/")
    monkeypatch.delenv("ATLAS_FLOW_ENVIRONMENT", raising=False)
    monkeypatch.setattr(flowtools, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(flowtools, "_token", lambda scopes: "dv-tok" if "dynamics" in scopes[0] else "flow-tok")
    return seen


def test_tools_follow_configuration(monkeypatch):
    for v in ("ATLAS_FLOW_ENABLED", "ATLAS_FLOW_WRITE", "ATLAS_FLOW_DATAVERSE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ATLAS_MS_CLIENT_ID", "app")
    assert flowtools.tools() == [] and flowtools.note() == ""
    monkeypatch.setenv("ATLAS_FLOW_ENABLED", "1")
    assert [x["name"] for x in flowtools.tools()] == ["flow_read"] and "off" in flowtools.note()
    monkeypatch.setenv("ATLAS_FLOW_WRITE", "1")
    assert [x["name"] for x in flowtools.tools()] == ["flow_read", "flow_run", "flow_toggle"]
    monkeypatch.setenv("ATLAS_FLOW_DATAVERSE_URL", "https://paga.crm.dynamics.com")
    assert [x["name"] for x in flowtools.tools()][-1] == "flow_create"
    monkeypatch.delenv("ATLAS_MS_CLIENT_ID")
    assert flowtools.tools() == []


def test_reads(api):
    text, ref = flowtools.read({"what": "flows"})
    assert ref == f"{ENV}/flows" and '"name":"Aviso cobranza vencida"' in text and '"trigger":"Request Button"' in text
    text, _ = flowtools.read({"what": "flow", "flow_id": FID})
    assert "Send_an_email" in text and "shared_office365" in text
    text, _ = flowtools.read({"what": "runs", "flow_id": FID})
    assert '"status":"Failed"' in text and "Unauthorized" in text
    text, _ = flowtools.read({"what": "environments"})
    assert '"default":true' in text
    for bad, msg in (({"what": "x"}, "what must be"), ({"what": "runs", "flow_id": "../etc"}, "flow_id must be")):
        with pytest.raises(flowtools.FlowToolError, match=msg):
            flowtools.read(bad)


def test_writes_are_validated_and_exact(api):
    with pytest.raises(flowtools.FlowToolError, match="motivo"):
        flowtools.plan_write("flow_run", {"flow_id": FID})
    run = flowtools.plan_write("flow_run", {"flow_id": FID, "motivo": "probar", "body": {"cliente": "1203"}})
    assert run.title == "Power Automate · ejecutar «Aviso cobranza vencida»" and '"cliente": "1203"' in run.detail
    assert "started" in flowtools.execute(run)
    call = next(s for s in api if "logic.azure.com" in s[1])
    assert call[0] == "POST" and call[3] == {"cliente": "1203"}

    off = flowtools.plan_write("flow_toggle", {"flow_id": FID, "on": False, "motivo": "falla diario"})
    assert "apagar" in off.title and "OFF" in flowtools.execute(off)
    assert any(s[1].split("?")[0].endswith(f"{FID}/stop") for s in api)

    with pytest.raises(flowtools.FlowToolError, match="trigger"):
        flowtools.plan_write("flow_create", {"name": "x", "definition": {"actions": {}}, "motivo": "m"})
    mk = flowtools.plan_write("flow_create", {"name": "Recordatorio L10", "motivo": "pedido en la junta",
                                              "definition": {"triggers": {"Recurrence": {"type": "Recurrence"}},
                                                             "actions": {"Post_message": {"type": "OpenApiConnection"}}},
                                              "connection_references": {"shared_teams": {"api": {"name": "shared_teams"}}}})
    assert "APAGADO" in mk.detail and "shared_teams" in mk.detail and "Recurrence" in mk.detail
    body = mk.args["body"]
    assert body["category"] == 5 and body["type"] == 1 and body["primaryentity"] == "none"
    cd = json.loads(body["clientdata"])
    assert cd["properties"]["definition"]["contentVersion"] == "1.0.0.0"
    assert "wf-123" in flowtools.execute(mk) and "DRAFT" in flowtools.execute(mk)
    assert any(s[1] == "https://paga.crm.dynamics.com/api/data/v9.2/workflows" for s in api)


def test_mission_runs_a_flow_after_approval_and_never_creates_a_rejected_one(api):
    registry = AgentRegistry.load()
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "alfred", "Flujos"), t("b", "sofia", "Contexto")))
    llm.when(task_call("Contexto"), report("ok"))
    llm.when(task_call("Flujos"),
             tool_use("flow_read", {"what": "runs", "flow_id": FID}),
             tool_use("flow_run", {"flow_id": FID, "motivo": "reintentar el aviso"}),
             tool_use("flow_create", {"name": "Nuevo", "motivo": "m", "definition": {
                 "triggers": {"manual": {"type": "Request"}}, "actions": {}}}),
             report("ran it; creation rejected"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm, max_turns=8)
        m = await engine.start("Revisa y reintenta el flujo de cobranza", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        assert not any("logic.azure.com" in s[1] for s in api)  # nothing ran while waiting
        await store.decide_approval(store.snapshot().approvals[0].id, "APPROVED")
        await until(lambda: len([a for a in store.snapshot().approvals if a.state == "PENDING"]) == 1)
        second = next(a for a in store.snapshot().approvals if a.state == "PENDING")
        await store.decide_approval(second.id, "REJECTED", note="todavía no")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store

    store = asyncio.run(go())
    assert "flow_run" in call_text(calls_matching(llm, stage("PLANNING"))[0])
    calls = calls_matching(llm, task_call("Flujos"))
    assert "Unauthorized" in call_text(calls[1])
    assert "started" in call_text(calls[2])
    assert "did NOT approve" in call_text(calls[3])
    assert not any("dynamics.com" in s[1] for s in api)
    refs = [e.ref for e in store.snapshot().evidence if e.kind == "external_call"]
    assert refs == [f"Power Automate GET {ENV}/flows/{FID}/runs", f"Power Automate {ENV}/flows/{FID}/run"]
