"""Corporate memory (docs/MEMORY.md): recall of earlier work, knowledge notes, Ask ATLAS."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from atlas.core.events import EventBus
from atlas.core.models import Claim, MissionReport
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live import memory as M
from atlas.live.ask import load_thread
from atlas.live.llm import call_text, tool_use

MODELS = ModelConfig(orchestrator="o", default="s", fast="f")
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


async def seed(store: WorldStore, objective: str, summary: str, *, topics: list[str], days_ago: int = 5,
               findings: list[str] | None = None) -> str:
    m = await store.create_mission(objective, "corporate", mode="live")
    report = MissionReport(
        mission_id=m.id, executive_summary=summary, objective_status="ACHIEVED",
        key_findings=[Claim(kind="FACT", statement=f, confidence="HIGH") for f in findings or []],
        next_actions=["Dirección: decidir precio de lista"], topics=topics,
        created_at=NOW - timedelta(days=days_ago),
    )
    await store.submit_mission_report(report)
    return m.id


def test_search_ranks_by_relevance_and_folds_accents():
    store = WorldStore(AgentRegistry.load(), EventBus())

    async def go():
        a = await seed(store, "Estudio de mercado Balcones 800", "Viable a 72,000 MXN/m²", topics=["Balcones 800"],
                       findings=["Absorción 2.1 unidades/mes"])
        b = await seed(store, "Programa de obra Torre Acqua", "Retraso de 6 semanas", topics=["Torre Acqua"])
        c = await seed(store, "Escrituración Amāra Centro", "41 de 60 escrituradas", topics=["Amāra"], days_ago=200)
        return a, b, c

    a, b, c = asyncio.run(go())
    entries = M.history_entries(store, "corporate")
    assert {e.id for e in entries} == {a, b, c}
    hits = M.search(entries, "actualiza el estudio de Balcones con la absorción", now=NOW)
    assert hits[0].id == a and b not in [h.id for h in hits]
    assert [h.id for h in M.search(entries, "¿cómo va amara?", now=NOW)] == [c]  # accents folded
    assert M.search(entries, "qué opinas del clima", now=NOW) == []
    assert M.search(entries, "", now=NOW) == []
    assert M.history_entries(store, "corporate", exclude={a}) and a not in {
        e.id for e in M.history_entries(store, "corporate", exclude={a})}
    block = M.memory_block(hits[:1], [])
    assert block.startswith(M.MEMORY_HEADER) and f"[{a}]" in block and "Topics: Balcones 800" in block
    assert "Next: Dirección: decidir precio de lista" in block


def test_recency_breaks_ties():
    store = WorldStore(AgentRegistry.load(), EventBus())

    async def go():
        old = await seed(store, "Estudio Pietra", "Precio 60,000", topics=["Pietra"], days_ago=300)
        new = await seed(store, "Estudio Pietra", "Precio 64,000", topics=["Pietra"], days_ago=2)
        return old, new

    old, new = asyncio.run(go())
    hits = M.search(M.history_entries(store, "corporate"), "estudio pietra", now=NOW)
    assert [h.id for h in hits] == [new, old]


def test_notes_file_roundtrip():
    n = M.add_note("corporate", "  Josué lleva Balcones desde septiembre. ")
    M.add_note("personal", "Nota personal")
    assert [x.text for x in M.active_notes("corporate")] == ["Josué lleva Balcones desde septiembre."]
    M.update_note(n.id, status="retired")
    assert M.active_notes("corporate") == []
    assert M.notes_text([]) == ""


def _llm(plan_objective: str) -> FakeLLM:
    plan = [{"ref": "a", "title": "Update", "description": "x", "assigned_to": "market-studies"},
            {"ref": "b", "title": "Package", "description": "y", "assigned_to": "alfred"}]
    rep = {"findings": [{"kind": "FACT", "statement": "ok", "confidence": "HIGH"}], "confidence": "HIGH"}
    llm = FakeLLM()
    llm.when(lambda kw: "STAGE: PLANNING" in call_text(kw), tool_use("create_plan", {"tasks": plan}))
    llm.when(lambda kw: "Your task: Update" in call_text(kw), tool_use("submit_report", {**rep, "asked_to": "a"}))
    llm.when(lambda kw: "Your task: Package" in call_text(kw), tool_use("submit_report", {**rep, "asked_to": "b"}))
    llm.when(lambda kw: "STAGE: REVIEW" in call_text(kw), tool_use("request_followups", {"tasks": []}))
    llm.when(lambda kw: "STAGE: CONSOLIDATION" in call_text(kw), tool_use("submit_mission_report", {
        "executive_summary": "Actualizado", "objective_status": "ACHIEVED", "topics": ["Balcones 800", " ", "Pietra"],
        "key_findings": [{"kind": "FACT", "statement": "ok", "confidence": "HIGH"}]}))
    return llm


def test_a_new_mission_recalls_earlier_work_and_notes(monkeypatch):
    reg = AgentRegistry.load()
    store = WorldStore(reg, EventBus())
    # MERCATO needs its private prompt to be available in tests
    p = M.paths.local_dir() / "agents" / "market-studies.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("You are MERCATO.", encoding="utf-8")
    M.add_note("corporate", "El comité pidió precio de lista máximo 75,000 MXN/m² para Balcones.")
    llm = _llm("x")

    async def go():
        prior = await seed(store, "Estudio de mercado Balcones 800", "Viable a 72,000 MXN/m²",
                           topics=["Balcones 800"], findings=["Absorción 2.1 unidades/mes"])
        other = await seed(store, "Programa de obra Torre Acqua", "Retraso", topics=["Torre Acqua"])
        engine = LiveEngine(store, loader=AgentLoader(reg, MODELS),
                            context=NodeContext(reg, local_dir=Path("/nonexistent")),
                            config=LiveConfig(models=MODELS, max_turns=3, audit=False, publish=False), llm=llm,
                            prices=PriceTable())
        m = await engine.start("Actualiza el estudio de Balcones 800 con precios de septiembre", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 10)
        return prior, other, m.id

    prior, other, mid = asyncio.run(go())
    plan_call = next(c for c in llm.calls if "STAGE: PLANNING" in call_text(c))
    task_call = next(c for c in llm.calls if "Your task: Update" in call_text(c))
    for c in (plan_call, task_call):
        text = call_text(c)
        assert "Prior knowledge" in text and f"[{prior}]" in text and "Viable a 72,000" in text
        assert f"[{other}]" not in text and f"[{mid}]" not in text
        assert "precio de lista máximo 75,000" in text  # the human's note
    logs = [e.summary for e in store.bus.history() if e.type == "log" and e.mission_id == mid]
    assert any(line.startswith(f"ATLAS recalls {prior}") for line in logs)
    report = store.mission_reports_for(mid)[-1]
    assert report.topics == ["Balcones 800", "Pietra"]
    # the finished mission is now part of the memory for the next one
    assert mid in {e.id for e in M.history_entries(store, "corporate")}


def test_ask_atlas_http(monkeypatch):
    from atlas.main import app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with TestClient(app) as client:
        store = app.state.store
        prior = client.portal.call(lambda: seed(store, "Estudio de mercado Balcones 800", "Viable a 72,000 MXN/m²",
                                                topics=["Balcones 800"]))
        llm = FakeLLM()
        llm.when(lambda kw: "STAGE: ASK" in call_text(kw), tool_use("answer", {
            "answer": "Según la misión del estudio, Balcones 800 es viable a 72,000 MXN/m².",
            "refs": [prior, "msn_invented"],
            "remember": ["El comité aprobó Balcones 800 el 24 de septiembre de 2026."],
            "suggest_mission": "Actualizar comparables de Balcones 800 con REDI",
        }), repeat=True)
        app.state.live.llm = llm
        app.state.live.backend = "api"
        out = client.post("/ask", json={"question": "¿Qué concluimos de Balcones? Ya lo aprobó el comité."}).json()
        assert [m["role"] for m in out] == ["human", "atlas"]
        answer = out[1]
        assert answer["refs"] == [{"id": prior, "kind": "mission", "title": "Estudio de mercado Balcones 800",
                                   "date": answer["refs"][0]["date"]}]
        assert answer["remember"] and answer["suggest_mission"].startswith("Actualizar comparables")
        prompt = call_text(llm.calls[0])
        assert "## Live state" in prompt and f"[{prior}]" in prompt and "Ya lo aprobó el comité" in prompt
        assert "Conversation mode (Ask ATLAS)" in prompt
        # second turn sees the conversation
        client.post("/ask", json={"question": "¿y el precio?"})
        assert "[Human] ¿Qué concluimos de Balcones?" in call_text(llm.calls[1])
        thread = client.get("/ask").json()
        assert len(thread) == 4 and len(load_thread("corporate")) == 4
        # save the proposed fact as a knowledge note
        note = client.post("/knowledge", json={"text": answer["remember"][0], "source": "ask"}).json()
        assert note["status"] == "active" and client.get("/knowledge").json()[0]["id"] == note["id"]
        assert client.patch(f"/knowledge/{note['id']}", json={"status": "retired"}).json()["status"] == "retired"
        assert client.patch("/knowledge/kn_nope", json={"status": "retired"}).status_code == 404
        idx = client.get("/memory", params={"q": "balcones"}).json()
        assert idx[0]["id"] == prior
        assert client.post("/ask/clear").json() == {"status": "cleared"}
        assert client.get("/ask").json() == []
        assert client.get("/ask", params={"node": "nope"}).status_code == 404


def test_ask_without_backend_lists_what_it_remembers(monkeypatch):
    from atlas.live.ask import ask

    store = WorldStore(AgentRegistry.load(), EventBus())

    class Off:
        def backend_info(self):
            from atlas.live.backend import BackendInfo

            return BackendInfo(None)

    async def go():
        prior = await seed(store, "Estudio de mercado Balcones 800", "Viable", topics=["Balcones 800"])
        return prior, await ask(Off(), store, "corporate", "¿qué sabemos de balcones?")

    prior, (_human, atlas) = asyncio.run(go())
    assert "Live agents are off" in atlas.text and f"[{prior}]" in atlas.text and atlas.refs[0].id == prior


def test_system_runs_are_not_indexed_as_missions():
    store = WorldStore(AgentRegistry.load(), EventBus())

    async def go():
        await seed(store, "Inbox scan · 2026-09-25 08:00", "3 follow-ups", topics=[])
        await seed(store, "L10 brief · 2026-W39", "brief", topics=[])
        return await seed(store, "Estudio Pietra", "ok", topics=["Pietra"])

    real = asyncio.run(go())
    assert [e.id for e in M.history_entries(store, "corporate")] == [real]
