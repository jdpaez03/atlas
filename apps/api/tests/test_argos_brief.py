"""ARGOS weekly L10 brief (docs/ARGOS.md § Weekly brief): computed numbers, ARGOS prose with a number guard, docx."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest
from test_argos_rocks import NOW, ROCKS

from atlas.argos.brief import SECTIONS, brief_file, build_brief, check_prose, gather
from atlas.argos.checks import CheckContext
from atlas.argos.rocks import rocks_path
from atlas.core.events import EventBus
from atlas.core.models import Alert, FollowUp
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore
from atlas.live import AgentLoader, FakeLLM, LiveConfig, ModelConfig, NodeContext, PriceTable
from atlas.live.files import extract_text
from atlas.live.llm import Meter, call_text, tool_use
from atlas.live.runtime import MissionScope

MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def alert(check, kind, sev, title, project=None, status="OPEN", detail=""):
    return Alert(check=check, kind=kind, severity=sev, title=title, project=project, status=status, detail=detail,
                 fingerprint=f"{check}:{kind}:{title}")


async def seed(store: WorldStore) -> None:
    for a in [
        alert("dashboards", "moved_date", "HIGH", "Entrega torre A movida", "Polanco", detail="15-oct → 30-nov"),
        alert("dashboards", "identical_report", "LOW", "Reporte idéntico a S38", "Polanco"),
        alert("dashboards", "missing_report", "MEDIUM", "Falta reporte S39", "Roma", status="ACKNOWLEDGED"),
        alert("dashboards", "value_change", "HIGH", "Ya resuelta", "Roma", status="RESOLVED"),
        alert("l10", "overdue_todo", "HIGH", "Overdue to-do · Enviar flujo", detail="Ana · due 2026-09-08"),
        alert("l10", "overdue_todo", "LOW", "Overdue to-do · Revisar permisos"),
        alert("l10", "unreported_todo", "LOW", "Unreported to-do · Cotizar elevadores"),
        alert("l10", "stale_issue", "MEDIUM", "Stale issue · Cambio de layout"),
    ]:
        await store.upsert_alert(a)
    for f in [
        FollowUp(kind="MY_COMMITMENT", title="Enviar presupuesto a Luis", counterpart="Luis", due=date(2026, 9, 20)),
        FollowUp(kind="REQUEST_TO_ME", title="Aprobar layout", due=date(2026, 9, 10), status="WAITING"),
        FollowUp(kind="AWAITING_REPLY", title="Respuesta del banco", due=date(2026, 9, 18)),
        FollowUp(kind="MY_COMMITMENT", title="Ya hecho", due=date(2026, 9, 1), status="DONE"),
        FollowUp(kind="MY_COMMITMENT", title="A tiempo", due=date(2026, 10, 1)),
    ]:
        await store.upsert_followup(f)


async def setup(registry, llm: FakeLLM) -> CheckContext:
    p = rocks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ROCKS, encoding="utf-8")
    store = WorldStore(registry, EventBus())
    mission = await store.create_mission("ARGOS brief · 2026-W39", "corporate", mode="live")
    await seed(store)
    loader = AgentLoader(registry, MODELS)
    scope = MissionScope(
        store=store, meter=Meter(llm, store, mission.id, PriceTable()), config=LiveConfig(models=MODELS),
        context=NodeContext(registry, local_dir=Path("/nonexistent-atlas-local")), mission_id=mission.id,
        objective=mission.objective, node="corporate", agents={"argos": loader.resolve("argos")},
        orchestrator=loader.resolve(registry.orchestrator.id),
    )
    return CheckContext(store=store, scope=scope, mail=None, config={}, now=NOW, mission_id=mission.id)


def prose(invented: bool) -> dict:
    return {
        "headline": ["Rocks: 3 of 6 on-track; Vender 100 departamentos en riesgo",
                     "Polanco: entrega de torre A movida (HIGH)",
                     "2 to-dos vencidos y 2 pendientes tuyos" + (" · 99 correos" if invented else "")],
        "sections": {
            "rocks": ["3 of 6 on-track", "AT RISK · Vender 100 departamentos (Luis): ritmo insuficiente",
                      "FAILED · Licencia de construcción (Pedro)"],
            "dashboards": ["Polanco: 2 alertas abiertas (1 HIGH)", "Roma: 1 alerta reconocida"],
            "l10": ["2 overdue to-dos: Enviar flujo (Ana), Revisar permisos", "1 stale issue: Cambio de layout"],
            "followups": ["2 overdue items of yours: presupuesto a Luis, aprobar layout"],
        },
    }


def is_brief(kw) -> bool:
    return "STAGE: ARGOS BRIEF" in call_text(kw)


def test_gather_computes_numbers(registry):
    async def go():
        c = await setup(registry, FakeLLM())
        return gather(c)

    facts = asyncio.run(go())
    assert facts.week == "2026-W39"
    assert facts.counts["rocks"] == 6 and facts.counts["rocks_on_track"] == 3
    assert facts.counts["rocks_at_risk"] == 1 and facts.counts["rocks_failed"] == 1
    assert facts.counts["dashboard_alerts"] == 3  # the resolved one is left out
    assert facts.counts["overdue_todo"] == 2 and facts.counts["unreported_todo"] == 1
    assert facts.counts["stale_issue"] == 1
    assert facts.counts["followups_overdue"] == 2 and facts.counts["followups_waiting_overdue"] == 1
    assert facts.lines["rocks"][0].startswith("3 of 6 on-track")
    assert facts.lines["rocks"][1].startswith("FAILED · Licencia")  # failed first, then at risk
    assert facts.lines["dashboards"][0] == "Polanco: 2 open alerts (1 HIGH)"
    assert any("(acknowledged)" in x for x in facts.lines["dashboards"])
    assert facts.lines["followups"][0] == "2 overdue items of yours"
    assert "Aprobar layout" in facts.lines["followups"][1]  # oldest first
    assert len(facts.headline) <= 3


def test_brief_with_argos_prose(registry):
    llm = FakeLLM()
    llm.when(is_brief, tool_use("write_brief", prose(invented=True)), tool_use("write_brief", prose(invented=False)))

    async def go():
        c = await setup(registry, llm)
        b = await build_brief(c)
        return c, b

    c, brief = asyncio.run(go())
    assert len(llm.calls) == 2
    retry = call_text(llm.calls[1])
    assert "these numbers are not in the facts: 99" in retry
    first = call_text(llm.calls[0])
    assert "3 of 6 on-track" in first and "Polanco: 2 open alerts (1 HIGH)" in first

    assert brief.week == "2026-W39" and brief.mission_id == c.mission_id
    assert brief.headline[0].startswith("Rocks: 3 of 6 on-track") and len(brief.headline) == 3
    assert set(brief.sections) == set(SECTIONS)
    assert brief.sections["l10"][0].startswith("2 overdue to-dos")

    att = brief.deliverable
    assert att is not None and att.name == "L10 brief 2026-W39.docx"
    assert att.download_url == f"/briefs/{brief.id}/file"
    path = brief_file(brief)
    assert path is not None and path.parent.name == "argos" and path.parent.parent.name == "corporate"
    text, _ = extract_text(path)
    for needle in ["L10 brief 2026-W39", "3 of 6 on-track", "Vender 100 departamentos", "AT RISK",
                   "Polanco: 2 alertas abiertas (1 HIGH)", "2 overdue items of yours", "12.5"]:
        assert needle in text, needle
    assert "99 correos" not in text

    state = c.store.snapshot()
    assert [b.id for b in state.briefs] == [brief.id]
    ev = c.store.evidence_for(c.mission_id)
    assert any(e.kind == "file_written" and e.ref.endswith("L10 brief 2026-W39.docx") for e in ev)

    # a second brief the same week never overwrites the first file
    async def again():
        llm2 = FakeLLM()
        llm2.when(is_brief, tool_use("write_brief", prose(invented=False)))
        c2 = await setup(registry, llm2)
        return await build_brief(c2)

    second = asyncio.run(again())
    assert second.deliverable is not None and second.deliverable.name == "L10 brief 2026-W39 (2).docx"
    assert brief_file(brief) == path


def test_brief_falls_back_to_computed_lines(registry):
    llm = FakeLLM()
    llm.when(is_brief, tool_use("write_brief", prose(invented=True)), repeat=True)

    async def go():
        c = await setup(registry, llm)
        return await build_brief(c)

    brief = asyncio.run(go())
    assert len(llm.calls) == 2
    assert brief.sections["rocks"][0].startswith("3 of 6 on-track")
    assert brief.sections["dashboards"][0] == "Polanco: 2 open alerts (1 HIGH)"
    text, _ = extract_text(brief_file(brief))
    assert "failed validation twice; computed lines used" in text


def test_check_prose_rules():
    from atlas.argos.brief import BriefFacts

    facts = BriefFacts(week="2026-W39", today=date(2026, 9, 24), counts={"rocks": 6, "rocks_on_track": 3},
                       lines={"rocks": ["3 of 6 on-track"], "dashboards": ["x"], "l10": ["y"], "followups": ["z"]})
    good = {"headline": ["a"], "sections": {"rocks": ["3 of 6 on-track"], "dashboards": ["n"], "l10": ["n"],
                                            "followups": ["n"]}}
    assert check_prose(good, facts) == []
    bad = {"headline": ["a", "b", "c", "d"], "sections": {"rocks": ["Todo bien, 7 de 6"], "dashboards": []}}
    errors = " | ".join(check_prose(bad, facts))
    assert "at most 3 lines" in errors and "sections.dashboards needs" in errors
    assert "must start with the line containing '3 of 6 on-track'" in errors
    assert re.search(r"not in the facts: 7\b", errors)
    assert check_prose(None, facts) == ["call write_brief"]


def test_brief_never_says_all_clear_for_unchecked_sources(tmp_path, monkeypatch):
    """Lead: an empty L10 section must say *why* when the check didn't run OK (not configured / failed / never)."""
    import json

    from atlas.argos import brief

    monkeypatch.setenv("ATLAS_LOCAL_DIR", str(tmp_path))
    (tmp_path / "argos").mkdir()
    state = tmp_path / "argos" / "state.json"

    def with_state(check_state, **extra):
        state.write_text(json.dumps({"runs": {"checks": {"l10": {"state": check_state, **extra}}}}))
        return brief._all_clear_or_why("l10", "ALL CLEAR")

    assert with_state("ok") == "ALL CLEAR"
    assert with_state("not configured", hint="Set ATLAS_SUITE_URL").startswith("Not checked: Set ATLAS_SUITE_URL")
    assert "incomplete" in with_state("failed", note="HTTP 500")
    state.unlink()
    assert brief._all_clear_or_why("l10", "ALL CLEAR").startswith("Not checked yet")
