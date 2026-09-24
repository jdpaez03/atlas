"""ARGOS Rocks check (docs/ARGOS.md § Rocks): statuses, paces, alerts, the example file, comment-preserving writes."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from atlas.argos import rocks as rocks_mod
from atlas.argos.checks import CheckContext, CheckNotConfigured
from atlas.argos.rocks import (
    RocksCheck,
    business_weeks,
    evaluate,
    quarter_bounds,
    rocks_path,
    two_owners,
    update_current,
)
from atlas.core.events import EventBus
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)  # Thu 24 Sep 2026, 11:00 Mexico City

ROCKS = """\
# My Q3 Rocks — keep this comment
quarter: 2026-Q3

rocks:
  - id: R1   # comfortable pace
    title: Escriturar 100 unidades
    owner: Ana
    project: Polanco
    due: 2026-09-30
    metric: unidades escrituradas
    start_value: 0
    target: 100
    current: 90   # updated weekly
  - id: R2
    title: Vender 100 departamentos
    owner: Luis
    project: Polanco
    due: 2026-09-30
    metric: departamentos vendidos
    target: 100
    current: 20
  - id: R3
    title: Licencia de construcción
    owner: Pedro
    due: 2026-09-15
    metric: licencias
    target: 1
    current: 0
  - id: R4
    title: Nuevo portal de proveedores
    owner: Rosa
    due: 2026-12-31
  - id: R5
    title: Reducir días de retraso
    owner: Ana / Luis
    due: 2026-12-31
    metric: días de retraso
    start_value: 30
    start_date: 2026-07-01
    target: 5
    current: 10
  - id: R6
    title: Cerrar crédito puente
    owner: Juan
    due: 2026-09-01
    done: true
  - title: Sin id
    owner: X
    due: 2026-12-31
"""


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


@pytest.fixture
def rocks_file():
    p = rocks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ROCKS, encoding="utf-8")
    yield p
    p.unlink(missing_ok=True)


def ctx(registry) -> CheckContext:
    return CheckContext(store=WorldStore(registry, EventBus()), scope=None, mail=None, config={}, now=NOW)


def test_helpers():
    assert quarter_bounds("2026-Q3") == (date(2026, 7, 1), date(2026, 9, 30))
    assert quarter_bounds("Q4 2026") == (date(2026, 10, 1), date(2026, 12, 31))
    assert quarter_bounds("otoño") is None
    assert business_weeks(date(2026, 9, 21), date(2026, 9, 28)) == 1.0  # Mon → next Mon = 5 weekdays
    assert business_weeks(date(2026, 9, 25), date(2026, 9, 28)) == 0.2  # Fri → Mon
    assert two_owners("Ana / Luis") and two_owners("Ana · Luis") and two_owners("Ana, Luis")
    assert not two_owners("Ana María López")


def test_rock_rules(rocks_file):
    ev = evaluate(NOW, rocks_file)
    by = {r.id: r for r in ev.rocks}
    assert set(by) == {"R1", "R2", "R3", "R4", "R5", "R6"}
    assert any("Rock #7 skipped: missing id" in n for n in ev.notes)

    r1 = by["R1"]
    assert r1.status == "ON_TRACK" and r1.quarter == "2026-Q3" and r1.start_date == date(2026, 7, 1)
    assert r1.required_pace == 12.5  # 10 left / 0.8 business weeks (Fri, Mon, Tue, Wed)
    assert r1.observed_pace is not None and r1.observed_pace >= 0.5 * r1.required_pace

    r2 = by["R2"]
    assert r2.status == "AT_RISK" and r2.required_pace == 100.0
    assert r2.observed_pace is not None and r2.observed_pace < 50
    assert "required" in r2.reason

    assert by["R3"].status == "FAILED" and "Past due 2026-09-15" in by["R3"].reason
    assert by["R4"].status == "UNKNOWN" and by["R4"].reason == "needs a measurable"
    r5 = by["R5"]  # a decreasing metric: 30 → 10 of 5 is good progress
    assert r5.status == "ON_TRACK" and r5.observed_pace is not None and r5.observed_pace > 0
    assert by["R6"].status == "DONE"  # done wins over past due

    alerts = {a.fingerprint: a for a in ev.alerts}
    assert alerts["rocks:rock-failed:r3"].kind == "rock_failed" and alerts["rocks:rock-failed:r3"].severity == "HIGH"
    at_risk = alerts["rocks:rock-at-risk:r2"]
    assert at_risk.kind == "rock_at_risk" and at_risk.severity == "HIGH"  # < 2 weeks left
    assert at_risk.project == "Polanco"
    assert at_risk.evidence[0].source == "rocks.yaml" and "id: R2" in at_risk.evidence[0].quote
    assert "current: 20" in at_risk.evidence[0].quote and "R3" not in at_risk.evidence[0].quote
    m = alerts["rocks:measurable:r4"]
    assert m.kind == "other" and m.severity == "LOW" and "measurable" in m.title
    owners = alerts["rocks:owner:r5"]
    assert owners.kind == "other" and owners.title.startswith("Rock with two owners has no owner")
    assert "rocks:rock-at-risk:r1" not in alerts and not any(":r6" in fp for fp in alerts)


def test_check_emits_rock_updated(registry, rocks_file):
    c = ctx(registry)
    result = asyncio.run(RocksCheck().run(c))
    assert result.notes[0].startswith("3 of 6 on-track")
    assert "1 at risk" in result.notes[0] and "1 failed" in result.notes[0]
    state = c.store.snapshot()
    assert {r.id: r.status for r in state.rocks}["R2"] == "AT_RISK"
    assert len(state.rocks) == 6
    # a second run with nothing changed emits nothing new
    assert asyncio.run(rocks_mod.emit(c.store, evaluate(NOW, rocks_file).rocks)) == 0


def test_missing_file_writes_example(registry):
    p = rocks_path()
    p.unlink(missing_ok=True)
    with pytest.raises(CheckNotConfigured) as exc:
        asyncio.run(RocksCheck().run(ctx(registry)))
    assert "example: true" in exc.value.hint and p.exists()
    text = p.read_text(encoding="utf-8")
    assert "# ARGOS · Rocks" in text and "Owner A" in text and "R2" in text
    with pytest.raises(CheckNotConfigured, match="still the example"):
        evaluate(NOW, p)
    # removing the flag makes the example itself valid
    p.write_text(text.replace("example: true\n", ""), encoding="utf-8")
    ev = evaluate(NOW, p)
    assert [r.id for r in ev.rocks] == ["R1", "R2"]
    assert {r.id: r.status for r in ev.rocks}["R2"] == "UNKNOWN"
    p.unlink()


def test_update_current_preserves_comments(rocks_file):
    rock = update_current("R2", 95, now=NOW, path=rocks_file)
    assert rock.current == 95 and rock.status == "ON_TRACK"
    text = rocks_file.read_text(encoding="utf-8")
    assert "# My Q3 Rocks — keep this comment" in text
    assert "- id: R1   # comfortable pace" in text and "current: 90   # updated weekly" in text
    assert "current: 95" in text and "current: 20" not in text
    assert text.count("\n") == ROCKS.count("\n")  # layout kept
    rock = update_current("R4", 2.5, now=NOW, path=rocks_file)  # adds the key
    assert rock.current == 2.5 and "current: 2.5" in rocks_file.read_text(encoding="utf-8")
    with pytest.raises(KeyError):
        update_current("nope", 1, now=NOW, path=rocks_file)
    with pytest.raises(ValueError):
        update_current("R1", "abc", now=NOW, path=rocks_file)  # type: ignore[arg-type]


def test_preflight(rocks_file):
    RocksCheck().preflight({})  # a real file: fine
    rocks_file.unlink()
    with pytest.raises(CheckNotConfigured):
        RocksCheck().preflight({})
    with pytest.raises(CheckNotConfigured, match="still the example"):
        RocksCheck().preflight({})
