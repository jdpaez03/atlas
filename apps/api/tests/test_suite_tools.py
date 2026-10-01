"""The agents' PAGA Suite tools (atlas/live/suitetools.py): reads, and writes that only happen after approval."""

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

from atlas.argos.suite import SuiteClient
from atlas.core.registry import AgentRegistry
from atlas.live import FakeLLM, suitetools
from atlas.live.llm import call_text, tool_use

CONTEXTO = {"semana": {"id": 7, "clave": "2026-S40", "estado": "abierta"}, "proyectos": ["Fiori"]}


@pytest.fixture
def suite(monkeypatch):
    seen: list[tuple[str, str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path + (f"?{req.url.query.decode()}" if req.url.query else ""), body))
        assert req.headers["Authorization"] == "Bearer tok"
        if req.method == "GET" and req.url.path.endswith("/l10/contexto"):
            return httpx.Response(200, json=CONTEXTO)
        if req.method == "GET":
            return httpx.Response(200, json={"todos": [{"codigo": "PC-001", "titulo": "Viejo"}]})
        if req.url.path.endswith("/importar"):
            return httpx.Response(200, json={"creados": len(body["items"]), "omitidos": 0,
                                             "advertencias": ["PC-031: nadie de «Pepe» está en la lista"]})
        if req.url.path.endswith("/cerrar"):
            return httpx.Response(200, json={"semana": {"clave": "2026-S40"}, "congeladas": 12})
        return httpx.Response(404)

    monkeypatch.setenv("ATLAS_SUITE_URL", "https://suite.test/api")
    monkeypatch.setenv("ATLAS_SUITE_TOKEN", "tok")
    monkeypatch.setenv("ATLAS_SUITE_WRITE", "1")
    monkeypatch.setattr(suitetools, "_client",
                        lambda: SuiteClient("https://suite.test/api", "tok", transport=httpx.MockTransport(handler)))
    return seen


def test_tools_follow_configuration(monkeypatch):
    for v in ("ATLAS_SUITE_URL", "ATLAS_SUITE_TOKEN", "ATLAS_SUITE_SCOPE", "ATLAS_SUITE_WRITE"):
        monkeypatch.delenv(v, raising=False)
    assert suitetools.tools() == [] and suitetools.note() == ""
    monkeypatch.setenv("ATLAS_SUITE_URL", "https://suite.test/api")
    monkeypatch.setenv("ATLAS_SUITE_TOKEN", "tok")
    assert [x["name"] for x in suitetools.tools()] == ["suite_read"]
    assert "off" in suitetools.note()
    with pytest.raises(suitetools.SuiteToolError, match="ATLAS_SUITE_WRITE"):
        suitetools.plan_write("suite_add_todos", {"todos": [{"titulo": "x"}]})
    monkeypatch.setenv("ATLAS_SUITE_WRITE", "1")
    assert [x["name"] for x in suitetools.tools()] == ["suite_read", "suite_add_todos", "suite_open_week",
                                                        "suite_close_week"]


def test_write_plans_are_exact_and_validated(suite):
    p = suitetools.plan_write("suite_add_todos", {"fuente": "Junta semanal 2026-09-28", "todos": [
        {"titulo": "  Enviar   minuta ", "responsable": "Ana", "fecha_compromiso": "2026-10-02",
         "contexto": "min 12", "codigo": "PC-001", "estado": "cumplido"},
        {"titulo": "Revisar precios Fiori", "proyecto": "SONOMA", "torre": "FIORI"}]})
    assert p.path == "/l10/admin/importar"
    assert p.body == {"items": [  # no codigo/estado can get through: only new to-dos
        {"titulo": "Enviar minuta", "responsable": "Ana", "fecha_compromiso": "2026-10-02", "contexto": "min 12",
         "origen": "junta"},
        {"titulo": "Revisar precios Fiori", "proyecto": "SONOMA", "torre": "FIORI", "origen": "junta"}]}
    assert "1. Enviar minuta · responsable: Ana · fecha: 2026-10-02" in p.detail
    assert "2. Revisar precios Fiori · responsable: (sin dueño) · fecha: (sin fecha) · proyecto: SONOMA · torre: FIORI" in p.detail
    for bad, msg in (({"todos": []}, "non-empty"), ({"todos": [{"responsable": "x"}]}, "no titulo"),
                     ({"todos": [{"titulo": "x", "fecha_compromiso": "el viernes"}]}, "not a date")):
        with pytest.raises(suitetools.SuiteToolError, match=msg):
            suitetools.plan_write("suite_add_todos", bad)
    c = suitetools.plan_write("suite_close_week", {"semana_id": "7", "clave": "2026-S40", "resumen": "12 de 14"})
    assert c.path == "/l10/admin/semanas/7/cerrar" and c.body is None and "congelan" in c.detail
    with pytest.raises(suitetools.SuiteToolError, match="resumen"):
        suitetools.plan_write("suite_close_week", {"semana_id": 7})
    o = suitetools.plan_write("suite_open_week", {"fecha_junta": "2026-10-05"})
    assert o.body == {"fecha_junta": "2026-10-05"}


def test_read(suite):
    text, ref = asyncio.run(suitetools.read({"what": "todos", "estado": "vivos"}))
    assert ref == "/l10/admin/todos?estado=vivos" and "PC-001" in text
    assert suite[-1][:2] == ("GET", "/api/l10/admin/todos?estado=vivos")
    with pytest.raises(suitetools.SuiteToolError, match="semana_id"):
        asyncio.run(suitetools.read({"what": "resumen"}))


def test_mission_adds_todos_after_approval_and_never_writes_a_rejected_close(suite):
    registry = AgentRegistry.load()
    rows = [{"titulo": "Enviar minuta", "responsable": "Ana", "fecha_compromiso": "2026-10-02"}]
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "alfred", "Cerrar semana L10"), t("b", "sofia", "Leer transcripcion")))
    llm.when(task_call("Leer transcripcion"), report("Ana: minuta"))
    llm.when(task_call("Cerrar semana L10"),
             tool_use("suite_read", {"what": "contexto"}),
             tool_use("suite_add_todos", {"todos": rows, "fuente": "Junta semanal"}),
             tool_use("suite_close_week", {"semana_id": 7, "clave": "2026-S40", "resumen": "12 de 14 reportados"}),
             report("1 to-do added; week left open"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm, max_turns=8)
        m = await engine.start("Cierra la semana y agrega los to-dos de la junta", "corporate")
        await until(lambda: any(a.state == "PENDING" for a in store.snapshot().approvals))
        first = store.snapshot().approvals[0]
        assert not [s for s in suite if s[0] == "POST"]  # nothing written while waiting
        await store.decide_approval(first.id, "APPROVED")
        await until(lambda: len([a for a in store.snapshot().approvals if a.state == "PENDING"]) == 1)
        second = next(a for a in store.snapshot().approvals if a.state == "PENDING")
        await store.decide_approval(second.id, "REJECTED", note="la cierro yo el lunes")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store

    store = asyncio.run(go())
    snap = store.snapshot()
    planning = calls_matching(llm, stage("PLANNING"))[0]
    assert "suite_add_todos" in call_text(planning)
    first, second = snap.approvals
    assert first.title == "PAGA Suite · dar de alta 1 to-do" and "Enviar minuta · responsable: Ana" in first.detail
    assert second.title == "PAGA Suite · cerrar la semana 2026-S40" and "12 de 14" in second.detail
    posts = [s for s in suite if s[0] == "POST"]
    assert posts == [("POST", "/api/l10/admin/importar", {"items": [{**rows[0], "origen": "junta"}]})]
    calls = calls_matching(llm, task_call("Cerrar semana L10"))
    assert "2026-S40" in call_text(calls[1])  # the read came back
    assert "1 to-do(s) created" in call_text(calls[2]) and "Pepe" in call_text(calls[2])
    assert "did NOT approve" in call_text(calls[3]) and "la cierro yo" in call_text(calls[3])
    refs = [(e.kind, e.ref, e.ok) for e in snap.evidence if e.kind == "external_call"]
    assert refs == [("external_call", "PAGA Suite GET /l10/contexto", True),
                    ("external_call", "PAGA Suite POST l10/admin/importar", True)]
