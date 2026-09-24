"""ARGOS framework (docs/ARGOS.md): engine, alert lifecycle, shared run gate, scheduler, brief, HTTP.

Checks here are fakes registered for the test; the LLM is FakeLLM (api backend).
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.argos import engine as engine_mod
from atlas.argos.checks import AlertDraft, CheckContext, CheckNotConfigured, CheckResult, fingerprint
from atlas.argos.config import RunState, load_watch, load_watch_with_error, watch_path
from atlas.argos.engine import (
    ArgosBusyError,
    ArgosEngine,
    CheckUnavailableError,
    register_brief_builder,
    register_check,
    registered_checks,
    unregister_check,
)
from atlas.argos.scheduler import ArgosScheduler, parse_brief, parse_schedule
from atlas.core import paths
from atlas.core.events import EventBus
from atlas.core.models import AlertEvidence, Attachment, Brief, RockStatus, WorldState
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.inbox.engine import InboxEngine, ScanBusyError
from atlas.inbox.state import user_tz
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live.evidence import record
from atlas.live.llm import call_text, tool_use

TZ = user_tz()
NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)  # Thursday 10:00 in Mexico City
MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def draft(key: str, title: str, *, severity="MEDIUM", project="Amāra", quote="30-sep", kind="moved_date",
          check="fake_a") -> AlertDraft:
    return AlertDraft(check=check, kind=kind, severity=severity, title=title, detail=f"detail of {key}",
                      project=project, fingerprint=fingerprint(check, project, key),
                      evidence=[AlertEvidence(source="S38.pdf", version="current", quote=quote)])


class ScriptedCheck:
    """Each run pops the next scripted step: a CheckResult, an exception, or a callable(ctx)."""

    def __init__(self, name: str, steps: list):
        self.name = name
        self.steps = steps
        self.contexts: list[CheckContext] = []

    async def run(self, ctx: CheckContext) -> CheckResult:
        self.contexts.append(ctx)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step(ctx)
            if asyncio.iscoroutine(step):
                step = await step
        return step


@pytest.fixture
def checks():
    """Register fake checks for one test: checks(name, steps, preflight=None) -> ScriptedCheck."""
    registered_checks()  # import the real check modules first, so ours win when a name collides
    before = dict(engine_mod._CHECKS)
    made: dict[str, ScriptedCheck] = {}

    def add(name: str, steps: list, preflight=None) -> ScriptedCheck:
        check = ScriptedCheck(name, steps)
        if preflight is not None:
            check.preflight = preflight
        made[name] = check
        register_check(name, lambda: check)
        return check

    yield add
    for name in list(engine_mod._CHECKS):
        unregister_check(name)
    engine_mod._CHECKS.update(before)
    register_brief_builder(None)


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def make(registry, *, llm=None, clock=None, gate=None) -> tuple[WorldStore, LiveEngine, ArgosEngine]:
    store = WorldStore(registry, EventBus())
    live = LiveEngine(store, loader=AgentLoader(registry, MODELS), config=LiveConfig(models=MODELS),
                      llm=llm or FakeLLM(), context=NodeContext(registry, local_dir=Path("/nonexistent")),
                      prices=PriceTable())
    argos = ArgosEngine(store, live, clock=clock or (lambda: NOW), gate=gate)
    return store, live, argos


def summaries(store: WorldStore, etype: str | None = None) -> list[str]:
    return [e.summary for e in store.bus.history() if etype is None or e.type == etype]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_watch_yaml_example_and_bad_yaml():
    cfg = load_watch()
    text = watch_path().read_text(encoding="utf-8")
    assert "stale_issue_days" in text and "lookback_days" in text and "expected" in text and "projects" in text
    assert cfg["dashboards"]["lookback_days"] == 10 and cfg["l10"]["stale_issue_days"] == 14
    assert r"S\d{2}" in cfg["dashboards"]["match"]
    watch_path().write_text("dashboards: [unclosed\n  - x: :", encoding="utf-8")
    cfg, err = load_watch_with_error()
    assert err and "not valid YAML" in err and cfg == {"dashboards": {}, "l10": {}, "rocks": {}}
    watch_path().write_text("l10:\n", encoding="utf-8")
    assert load_watch()["l10"] == {}


def test_run_state_preserves_other_keys(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"processed": {"m1": 1}}', encoding="utf-8")
    st = RunState.load(p)
    st.last_watch = NOW
    st.check("x").note = "hi"
    st.save()
    again = RunState.load(p)
    assert again.last_watch == NOW and again.checks["x"].note == "hi"
    assert '"processed"' in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# engine: alert lifecycle
# ---------------------------------------------------------------------------


def test_watch_mission_upsert_and_auto_resolve(registry, checks):
    x = draft("escrituraciones", "Escrituraciones moved 30-sep → 30-oct", severity="HIGH")
    y = draft("torre-b", "Torre B row removed", kind="removed_row")
    x2 = draft("escrituraciones", "Escrituraciones moved 30-sep → 30-oct", severity="HIGH", quote="30-oct")
    checks("fake_a", [CheckResult(alerts=[x, y], notes=["read 2 dashboards"]), CheckResult(alerts=[x2])])
    clock = {"now": NOW}

    async def go():
        store, _live, argos = make(registry, clock=lambda: clock["now"])
        initial = store.snapshot()
        m1 = await argos.run(["fake_a"])
        clock["now"] = NOW + timedelta(hours=6)
        m2 = await argos.run(["fake_a"])
        return store, argos, initial, m1, m2

    store, argos, initial, m1, m2 = asyncio.run(go())
    snap = store.snapshot()
    assert m1.objective == "ARGOS watch · fake_a · 2026-09-24 10:00" and m1.phase == "CLOSED" and m1.mode == "live"
    phases = [e.payload["mission"]["phase"] for e in store.bus.history(mission_id=m1.id)
              if e.type in ("mission.phase_changed", "mission.closed")]
    assert phases == ["DECOMPOSITION", "DELEGATION", "EXECUTION", "CONSOLIDATION", "REPORTING", "FOLLOW_UP",
                      "CLOSED"]
    t1 = store.tasks_for(m1.id)
    assert len(t1) == 1 and t1[0].assigned_to == "argos" and t1[0].status == "COMPLETED"
    assert t1[0].title == "Check fake_a"
    r1 = store.mission_reports_for(m1.id)[-1]
    assert r1.objective_status == "ACHIEVED"
    assert r1.executive_summary == "ARGOS watch · 2 new, 0 updated, 0 resolved alert(s) · 2 open"
    assert any("fake_a: 2 new, 0 updated, 0 resolved · read 2 dashboards" == c.statement for c in r1.key_findings)
    assert any("Escrituraciones" in a for a in r1.needs_human_attention)

    alerts = {a.fingerprint: a for a in snap.alerts}
    ax, ay = alerts[x.fingerprint], alerts[y.fingerprint]
    assert len(snap.alerts) == 2  # updated in place, never duplicated
    assert ax.status == "OPEN" and ax.first_seen == NOW and ax.last_seen == NOW + timedelta(hours=6)
    assert ax.evidence[0].quote == "30-oct" and ax.mission_id == m2.id and ax.check == "fake_a"
    assert ay.status == "RESOLVED"
    r2 = store.mission_reports_for(m2.id)[-1]
    assert r2.executive_summary.startswith("ARGOS watch · 0 new, 1 updated, 1 resolved")

    s = summaries(store, "alert.upserted")
    assert "Alert · HIGH · Amāra · Escrituraciones moved 30-sep → 30-oct" in s
    assert "Alert updated · HIGH · Amāra · Escrituraciones moved 30-sep → 30-oct" in s
    assert "Resolved · Torre B row removed (no longer detected)" in s

    # the reducer reproduces alerts; ARGOS back to MONITORING with its activity line
    assert fold(initial, store.bus.history()).alerts == snap.alerts
    st = store.agent_state("argos")
    assert st.status == "MONITORING" and st.activity.startswith("Watching ") and "fake_a" in st.activity
    assert st.activity.endswith(" · scheduled checks off")
    assert argos.status()["checks"][-1]["state"] == "ok" and argos.last_watch is None  # explicit list ≠ full


def test_failing_and_partial_checks_do_not_auto_resolve(registry, checks):
    x = draft("x", "Alert X")
    checks("fake_a", [CheckResult(alerts=[x]), RuntimeError("Suite unreachable"),
                      CheckResult(ok=False, notes=["2 files unreadable"])])
    checks("fake_b", [CheckResult()])

    async def go():
        store, _live, argos = make(registry)
        await argos.run(["fake_a"])
        m2 = await argos.run(["fake_a", "fake_b"])
        after_fail = store.alerts(status="OPEN")
        m3 = await argos.run(["fake_a"])
        return store, argos, m2, m3, after_fail

    store, argos, m2, m3, after_fail = asyncio.run(go())
    assert [a.title for a in after_fail] == ["Alert X"]
    tasks = {t.title: t for t in store.tasks_for(m2.id)}
    assert tasks["Check fake_a"].status == "FAILED" and tasks["Check fake_b"].status == "COMPLETED"
    r2 = store.mission_reports_for(m2.id)[-1]
    assert r2.objective_status == "PARTIAL" and "1 failed" in r2.executive_summary
    assert "fake_a: failed — Suite unreachable" in r2.needs_human_attention
    # ok=False: ran (task COMPLETED) but nothing auto-resolved
    assert [a.title for a in store.alerts(status="OPEN")] == ["Alert X"]
    assert store.tasks_for(m3.id)[0].status == "COMPLETED"
    st = {c["name"]: c for c in argos.status()["checks"]}
    assert st["fake_a"]["state"] == "partial" and st["fake_a"]["last_ok"] is False
    assert "2 files unreadable" in st["fake_a"]["note"]


def test_not_configured_and_preflight(registry, checks):
    hint = "set ATLAS_SUITE_URL and ATLAS_SUITE_TOKEN"
    checks("fake_suite", [CheckNotConfigured("PAGA Suite not configured", hint)])

    def pre(config):
        raise CheckNotConfigured("Needs a folder", "set FAKE_DIR in .env")

    checks("fake_pre", [CheckResult()], preflight=pre)
    checks("fake_ok", [CheckResult()])

    async def go():
        store, _live, argos = make(registry)
        m = await argos.run(["fake_suite", "fake_ok"])
        with pytest.raises(CheckUnavailableError) as exc:
            await argos.start_run(["fake_pre"])
        assert exc.value.hint == "set FAKE_DIR in .env" and "Needs a folder" in str(exc.value)
        with pytest.raises(CheckUnavailableError) as exc2:
            await argos.start_run(["nope"])
        assert "not installed" in str(exc2.value)
        return store, argos, m

    store, argos, m = asyncio.run(go())
    tasks = {t.title: t for t in store.tasks_for(m.id)}
    assert tasks["Check fake_suite"].status == "FAILED" and tasks["Check fake_ok"].status == "COMPLETED"
    report = store.mission_reports_for(m.id)[-1]
    assert f"fake_suite: not configured — {hint}" in report.needs_human_attention
    assert "1 not configured" in report.executive_summary and report.objective_status == "PARTIAL"
    st = {c["name"]: c for c in argos.status()["checks"]}
    assert st["fake_suite"]["state"] == "not configured" and st["fake_suite"]["hint"] == hint
    assert st["fake_suite"]["note"] == f"PAGA Suite not configured: {hint}"
    assert st["fake_pre"]["state"] == "not configured" and st["fake_pre"]["hint"] == "set FAKE_DIR in .env"
    agent_report = next(r for r in store.reports_for(m.id) if r.task_id == tasks["Check fake_suite"].id)
    assert any(hint in u for u in agent_report.unresolved)


def test_acknowledged_alerts_are_kept_and_resolved_ones_come_back_new(registry, checks):
    x, y = draft("x", "Alert X"), draft("y", "Alert Y")
    checks("fake_a", [CheckResult(alerts=[x, y]), CheckResult(), CheckResult(alerts=[x, y])])

    async def go():
        store, _live, argos = make(registry)
        await argos.run(["fake_a"])
        ax = next(a for a in store.alerts() if a.title == "Alert X")
        await store.set_alert_status(ax.id, "ACKNOWLEDGED")
        await argos.run(["fake_a"])  # sees nothing: Y resolves, X (acknowledged) is left alone
        mid = store.snapshot().alerts
        await argos.run(["fake_a"])  # both again: X updated (still acknowledged), Y is a NEW alert
        return store, ax, mid

    store, ax, mid = asyncio.run(go())
    assert {a.title: a.status for a in mid} == {"Alert X": "ACKNOWLEDGED", "Alert Y": "RESOLVED"}
    alerts = store.alerts()
    assert len(alerts) == 3
    assert store.alert(ax.id).status == "ACKNOWLEDGED"
    ys = sorted((a for a in alerts if a.title == "Alert Y"), key=lambda a: a.status)
    assert [a.status for a in ys] == ["OPEN", "RESOLVED"] and ys[0].id != ys[1].id
    s = summaries(store, "alert.upserted")
    assert "Alert acknowledged · MEDIUM · Amāra · Alert X" in s
    assert "Alert still acknowledged · MEDIUM · Amāra · Alert X" in s


def test_check_context_llm_step_and_evidence(registry, checks):
    llm = FakeLLM()
    llm.when(lambda kw: "STAGE: ARGOS FAKE" in call_text(kw), tool_use("record_x", {"value": 7}))
    tool = {"name": "record_x", "description": "x", "input_schema": {"type": "object", "properties": {}}}

    async def step(ctx: CheckContext):
        assert ctx.scope is not None and ctx.executor is not None and ctx.task_id and ctx.mission_id
        assert ctx.config["l10"]["stale_issue_days"] == 14 and ctx.now == NOW and ctx.node == "corporate"
        data = await ctx.executor.structured(ctx.scope, model="claude-sonnet-test", system=["STAGE: ARGOS FAKE"],
                                             prompt="go", tool=tool, max_tokens=100)
        await record(ctx.store, mission_id=ctx.mission_id, task_id=ctx.task_id, agent_id="argos",
                     kind="file_read", ref="dashboards/Amara/2026-W39/S39.pdf")
        return CheckResult(notes=[f"value {data['value']}"])

    checks("fake_llm", [step])

    async def go():
        store, _live, argos = make(registry, llm=llm)
        return store, await argos.run(["fake_llm"])

    store, m = asyncio.run(go())
    assert len(llm.calls) == 1 and store.mission(m.id).usage.llm_calls == 1
    rep = store.reports_for(m.id)[0]
    assert [e.kind for e in rep.evidence] == ["file_read"] and rep.limitations == ["value 7"]


# ---------------------------------------------------------------------------
# shared gate with the inbox engine
# ---------------------------------------------------------------------------


def test_run_gate_shared_with_inbox_scan(registry, checks):
    async def go():
        release = asyncio.Event()

        async def slow(ctx):
            await release.wait()
            return CheckResult()

        checks("fake_slow", [slow])
        store, live, argos = make(registry)
        inbox = InboxEngine(store, live, source=None, clock=lambda: NOW, gate=argos.gate)

        class Src:
            name = "fake"

            async def status(self):
                from atlas.inbox.sources.base import SourceStatus
                return SourceStatus(name="fake", connected=True)

        inbox.source = Src()
        m = await argos.start_run(["fake_slow"])
        await asyncio.sleep(0.01)
        with pytest.raises(ScanBusyError) as exc:
            await inbox.start_scan()
        assert "ARGOS watch is running" in str(exc.value)
        with pytest.raises(ArgosBusyError):
            await argos.start_run(["fake_slow"])
        register_brief_builder(lambda ctx: Brief(week="2026-W39"))
        with pytest.raises(ArgosBusyError):
            await argos.start_brief()
        st = argos.status()
        assert st["running"] and st["mission_id"] == m.id and st["busy"]
        release.set()
        await asyncio.wait_for(argos.wait(), 5)
        assert not argos.gate.busy
        # and the other way round: an inbox scan holds the gate
        gate_task = asyncio.create_task(asyncio.sleep(10))
        argos.gate.hold("An inbox scan", "msn_x", gate_task)
        with pytest.raises(ArgosBusyError) as exc2:
            await argos.start_run(["fake_slow"])
        assert "An inbox scan is running" in str(exc2.value)
        gate_task.cancel()
        return store, m

    store, m = asyncio.run(go())
    assert store.mission(m.id).phase == "CLOSED"


def test_cancelled_watch_stops_cleanly(registry, checks):
    async def go():
        release = asyncio.Event()

        async def slow(ctx):
            await release.wait()
            return CheckResult(alerts=[draft("x", "X")])

        checks("fake_slow", [slow])
        checks("fake_z", [CheckResult(alerts=[draft("y", "Y", check="fake_z")])])
        store, _live, argos = make(registry)
        m = await argos.start_run(["fake_slow", "fake_z"])
        await asyncio.sleep(0.01)
        await store.cancel_mission(m.id)
        release.set()
        await asyncio.wait_for(argos.wait(), 5)
        return store, m

    store, m = asyncio.run(go())
    assert store.mission(m.id).phase == "CLOSED"
    assert store.alerts() == []  # the slow check's result is dropped and fake_z never ran
    assert store.agent_state("argos").status == "MONITORING"


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


def test_brief_mission(registry, checks):
    out = paths.local_dir() / "outputs" / "corporate" / "argos"

    async def builder(ctx: CheckContext) -> Brief:
        assert ctx.task_id and ctx.mission_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "L10 brief 2026-W39.docx").write_bytes(b"PK-fake-docx")
        return Brief(week="2026-W39", headline=["5 of 7 Rocks on-track"], sections={"Rocks": ["a", "b"]},
                     deliverable=Attachment(name="L10 brief 2026-W39.docx", kind="file",
                                            uri=str(out / "L10 brief 2026-W39.docx")))

    register_brief_builder(builder)

    async def go():
        store, _live, argos = make(registry)
        m = await argos.start_brief()
        await asyncio.wait_for(argos.wait(), 5)
        return store, argos, m

    store, argos, m = asyncio.run(go())
    assert m.objective == "L10 brief · 2026-W39" and store.mission(m.id).phase == "CLOSED"
    b = store.briefs()[0]
    assert b.mission_id == m.id and b.deliverable.download_url == f"/briefs/{b.id}/file"
    assert "L10 brief 2026-W39 ready · 5 of 7 Rocks on-track" in summaries(store, "brief.ready")
    rep = store.mission_reports_for(m.id)[-1]
    assert rep.objective_status == "ACHIEVED" and rep.deliverables[0].name == "L10 brief 2026-W39.docx"
    assert argos.last_brief == NOW

    async def failing(ctx):
        raise RuntimeError("no Rocks file")

    register_brief_builder(failing)

    async def go2():
        store, _live, argos = make(registry)
        argos.state.last_brief = None
        m = await argos.start_brief()
        await asyncio.wait_for(argos.wait(), 5)
        return store, argos, m

    store, argos, m = asyncio.run(go2())
    rep = store.mission_reports_for(m.id)[-1]
    assert rep.objective_status == "NOT_ACHIEVED" and "no Rocks file" in rep.executive_summary
    assert argos.last_brief is None and store.tasks_for(m.id)[0].status == "FAILED"


def test_brief_builder_missing(registry, monkeypatch):
    monkeypatch.setattr(engine_mod, "brief_builder", lambda: None)

    async def go():
        _store, _live, argos = make(registry)
        with pytest.raises(CheckUnavailableError) as exc:
            await argos.start_brief()
        return exc.value

    exc = asyncio.run(go())
    assert "brief.py" in exc.hint


# ---------------------------------------------------------------------------
# store summaries
# ---------------------------------------------------------------------------


def test_rock_and_brief_summaries(registry):
    async def go():
        store = WorldStore(registry, EventBus())
        base = {"title": "Escriturar 51", "owner": "Ana", "quarter": "2026-Q3", "due": date(2026, 9, 30)}
        await store.upsert_rock(RockStatus(id="R1", status="AT_RISK", observed_pace=2.0, required_pace=5.3, **base))
        await store.upsert_rock(RockStatus(id="R2", status="UNKNOWN", reason="needs a measurable", **base))
        await store.upsert_brief(Brief(week="2026-W40"))
        return store

    store = asyncio.run(go())
    assert summaries(store, "rock.updated") == ["Rock R1 · AT_RISK · pace 2.0/wk vs 5.3 required",
                                                "Rock R2 · UNKNOWN · needs a measurable"]
    assert summaries(store, "brief.ready") == ["L10 brief 2026-W40 ready"]
    assert [r.id for r in store.rocks()] == ["R1", "R2"]


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


class FakeArgos:
    def __init__(self, clock, last_watch=None, last_brief=None, busy_times: int = 0):
        from atlas.core.rungate import RunGate

        self.tz = TZ
        self.clock = clock
        self.last_watch = last_watch
        self.last_brief = last_brief
        self.scheduler = None
        self.gate = RunGate()
        self.busy_times = busy_times
        self.calls: list[tuple[datetime, str, str]] = []

    def now(self):
        return self.clock()

    async def set_idle(self):
        return None

    async def wait(self):
        return None

    async def start_run(self, checks=None, trigger="manual"):
        if self.busy_times:
            self.busy_times -= 1
            raise ArgosBusyError("An inbox scan is running", "wait")
        self.calls.append((self.clock().astimezone(TZ), "watch", trigger))
        self.last_watch = self.clock()

    async def start_brief(self, trigger="manual"):
        self.calls.append((self.clock().astimezone(TZ), "brief", trigger))
        self.last_brief = self.clock()


def _run(engine, box, *, until: int, times="09:30,16:00", brief="MON 07:30", max_sleeps=400, retry=120):
    sleeps = {"n": 0}

    async def fake_sleep(seconds):
        sleeps["n"] += 1
        box["now"] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def go():
        sched = ArgosScheduler(engine, parse_schedule(times), parse_brief(brief), clock=lambda: box["now"],
                               sleep=fake_sleep, poll=3600, retry=retry, startup_delay=0)
        await sched.start()
        while len(engine.calls) < until and sleeps["n"] < max_sleeps:
            await asyncio.sleep(0)
        await sched.stop()
        return sched

    return asyncio.run(go())


def test_schedule_parsing():
    assert [t.strftime("%H:%M") for t in parse_schedule(None)] == ["09:30", "16:00"]
    assert parse_schedule("") == [] and parse_brief("") == []
    assert parse_brief(None) == parse_brief("MON 07:30") and parse_brief("mon 7:30")[0][0] == 0
    assert [d for d, _ in parse_brief("FRI 17:00, lun 08:00, bogus")] == [0, 4]
    sched = ArgosScheduler(FakeArgos(lambda: NOW), parse_schedule(None), parse_brief(None))
    assert sched.next_slot(local(2026, 9, 25, 16, 30)) == local(2026, 9, 28, 9, 30)
    assert sched.previous_brief_slot(local(2026, 9, 27, 12)) == local(2026, 9, 21, 7, 30)
    assert sched.next_brief_slot(local(2026, 9, 24, 12)) == local(2026, 9, 28, 7, 30)
    assert sched.brief_labels() == ["MON 07:30"] and sched.schedule_labels() == ["09:30", "16:00"]


def test_scheduler_slots_and_brief():
    box = {"now": local(2026, 9, 27, 20)}  # Sunday evening: last watch Fri 16:30, brief done Monday
    engine = FakeArgos(lambda: box["now"], last_watch=local(2026, 9, 25, 16, 30), last_brief=local(2026, 9, 21, 8))
    _run(engine, box, until=4)
    assert engine.calls == [
        (local(2026, 9, 28, 7, 30), "brief", "scheduled"),
        (local(2026, 9, 28, 9, 30), "watch", "scheduled"),
        (local(2026, 9, 28, 16), "watch", "scheduled"),
        (local(2026, 9, 29, 9, 30), "watch", "scheduled"),
    ]


def test_scheduler_catch_up_watch_then_brief():
    box = {"now": local(2026, 9, 28, 10)}  # Monday 10:00, nothing ever ran
    engine = FakeArgos(lambda: box["now"])
    _run(engine, box, until=3)
    assert engine.calls == [
        (local(2026, 9, 28, 10), "watch", "catch-up"),
        (local(2026, 9, 28, 10), "brief", "catch-up"),
        (local(2026, 9, 28, 16), "watch", "scheduled"),
    ]


def test_scheduler_retries_when_busy():
    box = {"now": local(2026, 9, 28, 9, 0)}
    engine = FakeArgos(lambda: box["now"], last_watch=local(2026, 9, 25, 16, 30), last_brief=local(2026, 9, 28, 8),
                       busy_times=2)
    _run(engine, box, until=1, retry=120)
    assert engine.calls == [(local(2026, 9, 28, 9, 34), "watch", "scheduled")]  # 09:30 busy, +2 min busy, then ok


def test_scheduler_disabled_fast_stop_and_activity_line(registry, checks):
    checks("fake_a", [CheckResult()])

    async def go():
        off = ArgosScheduler(FakeArgos(lambda: NOW), [], [])
        await off.start()
        assert not off.running and off.next_run() is None
        store, _live, argos = make(registry, clock=lambda: NOW)
        argos.state.last_watch = NOW  # no catch-up
        argos.state.last_brief = NOW
        on = ArgosScheduler(argos, parse_schedule("09:30,16:00"), parse_brief("MON 07:30"), startup_delay=0)
        await on.start()
        await asyncio.sleep(0.02)
        assert on.running and on.next_run() == local(2026, 9, 24, 16) and on.next_brief() == local(2026, 9, 28, 7, 30)
        line = argos.activity_line()
        state = store.agent_state("argos")
        t0 = time.monotonic()
        await on.stop()
        return line, state, time.monotonic() - t0, argos.status()

    line, state, elapsed, st = asyncio.run(go())
    assert line.endswith("· next check 16:00") and "fake_a" in line
    assert state.status == "MONITORING" and state.activity == line
    assert elapsed < 0.5 and st["next_run"] is None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _poll(client: TestClient, pred, timeout: float = 5.0) -> WorldState:
    deadline = time.monotonic() + timeout
    while True:
        state = WorldState.model_validate(client.get("/state").json())
        if pred(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _closed(mid):
    return lambda s: next(m for m in s.missions if m.id == mid).phase == "CLOSED"


def test_http_argos(monkeypatch, checks):
    from atlas.main import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    x, y = draft("x", "Alert X", severity="HIGH"), draft("y", "Alert Y", project="Nativa")
    checks("fake_a", [CheckResult(alerts=[x, y])])

    def pre(config):
        raise CheckNotConfigured("PAGA Suite not configured", "set ATLAS_SUITE_URL and ATLAS_SUITE_TOKEN")

    checks("fake_pre", [CheckResult()], preflight=pre)
    base = {"title": "Escriturar 51", "owner": "Ana", "quarter": "2026-Q3", "due": "2026-09-30"}

    async def rocks_step(ctx):
        await ctx.store.upsert_rock(RockStatus(id="R1", status="ON_TRACK", **base), mission_id=ctx.mission_id,
                                    agent_id="argos")
        return CheckResult()

    checks("rocks", [rocks_step])

    with TestClient(app) as client:
        assert app.state.inbox.gate is app.state.argos.gate
        st = client.get("/argos/status").json()
        names = {c["name"]: c for c in st["checks"]}
        assert names["fake_a"]["installed"] and names["fake_a"]["state"] == "never run"
        assert {"dashboards", "l10", "rocks"} <= set(names) and st["running"] is False
        assert st["schedule"] == [] and st["next_run"] is None  # disabled by the test env

        r = client.post("/argos/run", json={"checks": ["fake_a"]})
        assert r.status_code == 200 and r.json()["objective"].startswith("ARGOS watch · fake_a · ")
        _poll(client, _closed(r.json()["id"]))
        alerts = client.get("/alerts").json()
        assert [a["title"] for a in alerts] == ["Alert X", "Alert Y"]  # HIGH first
        assert [a["title"] for a in client.get("/alerts", params={"project": "Nativa"}).json()] == ["Alert Y"]
        assert len(client.get("/alerts", params={"check": "fake_a", "status": "OPEN"}).json()) == 2
        r = client.patch(f"/alerts/{alerts[0]['id']}", json={"status": "ACKNOWLEDGED"})
        assert r.status_code == 200 and r.json()["status"] == "ACKNOWLEDGED"
        assert client.patch(f"/alerts/{alerts[0]['id']}", json={"status": "NOPE"}).status_code == 422
        assert client.patch("/alerts/alr_nope", json={"status": "RESOLVED"}).status_code == 404
        assert client.get("/alerts", params={"status": "ACKNOWLEDGED"}).json()[0]["title"] == "Alert X"

        r = client.post("/argos/run", json={"checks": ["nope"]})
        assert r.status_code == 409 and "not installed" in r.json()["detail"]
        r = client.post("/argos/run", json={"checks": ["fake_pre"]})
        assert r.status_code == 409 and "ATLAS_SUITE_URL" in r.json()["detail"]
        st = {c["name"]: c for c in client.get("/argos/status").json()["checks"]}
        assert st["fake_pre"]["state"] == "not configured" and "ATLAS_SUITE_TOKEN" in st["fake_pre"]["note"]
        assert st["fake_a"]["state"] == "ok" and st["fake_a"]["last_ok"] is True

        # busy: a slow run holds the shared gate
        release = {"go": False}

        async def slow(ctx):
            while not release["go"]:
                await asyncio.sleep(0.01)
            return CheckResult()

        checks("fake_slow", [slow])
        r = client.post("/argos/run", json={"checks": ["fake_slow"]})
        assert r.status_code == 200
        r2 = client.post("/argos/run", json={"checks": ["fake_a"]})
        assert r2.status_code == 409 and "ARGOS watch is running" in r2.json()["detail"]
        assert client.get("/argos/status").json()["running"] is True
        assert client.post("/rocks/reload").status_code == 409
        release["go"] = True
        _poll(client, _closed(r.json()["id"]))

        # Rocks
        assert client.get("/rocks").json() == []
        r = client.post("/rocks/reload")
        assert r.status_code == 200 and [x["id"] for x in r.json()] == ["R1"]
        fake_rocks = types.ModuleType("atlas.argos.rocks")
        monkeypatch.setitem(sys.modules, "atlas.argos.rocks", fake_rocks)
        assert client.patch("/rocks/R1", json={"current": 12}).status_code == 501

        def update_current(rock_id, value):
            if rock_id != "R1":
                raise KeyError(rock_id)
            return RockStatus(id="R1", status="AT_RISK", current=value, observed_pace=2.0, required_pace=5.3, **base)

        fake_rocks.update_current = update_current
        r = client.patch("/rocks/R1", json={"current": 12})
        assert r.status_code == 200 and r.json()["current"] == 12 and r.json()["status"] == "AT_RISK"
        assert client.patch("/rocks/R9", json={"current": 1}).status_code == 404
        assert client.get("/rocks").json()[0]["status"] == "AT_RISK"

        # brief
        monkeypatch.setattr(engine_mod, "brief_builder", lambda: None)
        r = client.post("/argos/brief")
        assert r.status_code == 409 and "brief.py" in r.json()["detail"]
        out = paths.local_dir() / "outputs" / "corporate" / "argos"

        async def builder(ctx):
            out.mkdir(parents=True, exist_ok=True)
            (out / "L10 brief 2026-W39.docx").write_bytes(b"PK-fake-docx")
            return Brief(week="2026-W39", headline=["All good"],
                         deliverable=Attachment(name="L10 brief 2026-W39.docx", kind="file"))

        monkeypatch.setattr(engine_mod, "brief_builder", lambda: builder)
        r = client.post("/argos/brief")
        assert r.status_code == 200 and r.json()["objective"] == "L10 brief · 2026-W39"
        _poll(client, _closed(r.json()["id"]))
        briefs = client.get("/briefs").json()
        assert len(briefs) == 1 and briefs[0]["week"] == "2026-W39"
        assert client.get(f"/briefs/{briefs[0]['id']}").json()["headline"] == ["All good"]
        f = client.get(briefs[0]["deliverable"]["download_url"])
        assert f.status_code == 200 and f.content == b"PK-fake-docx"
        assert "wordprocessingml" in f.headers["content-type"]
        assert client.get("/briefs/brf_nope/file").status_code == 404

        events = client.get("/events").json()
        types_ = {e["type"] for e in events}
        assert {"alert.upserted", "rock.updated", "brief.ready"} <= types_
        argos_state = next(s for s in client.get("/state").json()["agent_states"] if s["agent_id"] == "argos")
        assert argos_state["status"] == "MONITORING" and argos_state["activity"].startswith("Watching ")


def test_hung_check_times_out_and_frees_the_lock(registry, checks, monkeypatch):
    """Lead: a check that hangs must fail after ATLAS_ARGOS_CHECK_TIMEOUT and release the shared lock."""
    monkeypatch.setenv("ATLAS_ARGOS_CHECK_TIMEOUT", "0.2")

    async def hang(ctx):
        await asyncio.sleep(3600)

    checks("fake_a", [hang])
    checks("fake_b", [CheckResult(alerts=[draft("y", "Alert Y", check="fake_b")])])

    async def go():
        store, _live, argos = make(registry)
        m = await asyncio.wait_for(argos.run(["fake_a", "fake_b"]), timeout=10)
        return store, argos, m

    store, argos, m = asyncio.run(go())
    tasks = {t.title: t for t in store.tasks_for(m.id)}
    assert tasks["Check fake_a"].status == "FAILED" and tasks["Check fake_b"].status == "COMPLETED"
    assert "timed out" in {c["name"]: c for c in argos.status()["checks"]}["fake_a"]["note"]
    assert [a.title for a in store.alerts(status="OPEN")] == ["Alert Y"]
    assert not argos.status()["running"]


def test_dashboards_preflight_without_mail_source(monkeypatch):
    """Lead: with no mailbox configured, scheduled watches skip dashboards instead of failing a mission."""
    import atlas.argos.dashboards as dash
    from atlas.argos.checks import CheckNotConfigured
    from atlas.inbox import sources

    monkeypatch.setattr(sources, "make_source", lambda *a, **k: None)
    with pytest.raises(CheckNotConfigured) as exc:
        dash.DashboardsCheck().preflight({})
    assert "Connect Outlook" in exc.value.hint
