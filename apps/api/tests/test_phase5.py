"""Phase 5: AUDITOR (audit + one revision round + untraced figures), task retries, resume, per-agent usage."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
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
)

from atlas.core.models import AuditVerdict
from atlas.core.registry import AgentRegistry
from atlas.live import FakeLLM, LLMError
from atlas.live.auditor import Precheck, precheck, untraced_figures
from atlas.live.llm import call_text, tool_use

AUDIT = stage("AUDIT")


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def audit(*entries: tuple[str, str, list[dict] | None]):
    return tool_use("submit_audit", {"audits": [
        {"report_ref": ref, "verdict": verdict, "summary": f"{ref} {verdict.lower()}", "issues": issues or []}
        for ref, verdict, issues in entries
    ]})


ISSUE = {"finding": "Average rent is 18k MXN", "problem": "No listing was opened; cite the source you read",
         "kind": "unsupported", "severity": "HIGH"}


def run(registry, llm, **cfg):
    async def go():
        store, engine = make_live(registry, llm, audit=True, **cfg)
        m = await engine.start("Evaluate the Polanco 2BR", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store, engine, m.id

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# AUDITOR
# ---------------------------------------------------------------------------


def test_audit_fail_opens_one_revision_and_final_audit(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("research", "sofia", "Market research"),
                                     t("check", "argos", "Consistency check", ["research"])))
    llm.when(task_call("Market research"), report("Average rent is 18k MXN"))
    llm.when(task_call("Consistency check"), report("Pro-forma matches"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(task_call("Revise: Market research"), report("Average rent is 17.5k MXN per the listing I read"))
    # first audit: R1 = market research FAIL, R2 = check PASS; then the revision audited as final
    llm.when(AUDIT, audit(("R1", "FAIL", [ISSUE]), ("R2", "PASS", None)), audit(("R1", "PASS", None)))
    llm.when(stage("CONSOLIDATION"), mission_report("Rent is 17.5k MXN; pro-forma matches."))

    store, _engine, mid = run(registry, llm)
    snap = store.snapshot()
    tasks = {x.title: x for x in snap.tasks}
    rev = tasks["Revise: Market research"]
    assert rev.revision_of == tasks["Market research"].id and rev.depends_on == [tasks["Market research"].id]
    assert rev.status == "COMPLETED" and rev.assigned_to == "sofia"
    # the revision task saw the auditor's issue and its own original report
    first = call_text(calls_matching(llm, task_call("Revise: Market research"))[0])
    assert "REVISION REQUESTED BY AUDITOR" in first and "cite the source you read" in first
    assert "Average rent is 18k MXN" in first
    audits = store.audits_for(mid)
    assert [(a.verdict, a.final) for a in audits] == [
        (AuditVerdict.FAIL, False), (AuditVerdict.PASS, False), (AuditVerdict.PASS, True)]
    assert audits[0].revision_task_id == rev.id and audits[0].issues[0].kind == "unsupported"
    assert audits[0].checks  # the system's deterministic checks travel with the audit
    # the auditor prompt carried the evidence section and the system checks
    audit_prompt = call_text(calls_matching(llm, AUDIT)[0])
    assert "Evidence recorded by ATLAS for this task" in audit_prompt and "System checks already run" in audit_prompt
    # consolidation saw the AUDIT lines and the instruction to use the revision
    consolidation = call_text(calls_matching(llm, stage("CONSOLIDATION"))[0])
    assert "AUDIT: FAIL" in consolidation and "use the revised report" in consolidation
    final = snap.mission_reports[-1]
    assert "3 report(s)" not in final.audit_summary  # the revision is not double-counted
    assert "checked 2 report(s)" in final.audit_summary and "1 sent back for revision" in final.audit_summary
    assert "after revision 1 of 1 held up" in final.audit_summary
    assert any(e.type == "audit.recorded" for e in store.bus.history())
    auditor = next(s for s in snap.agent_states if s.agent_id == "auditor")
    assert auditor.status == "IDLE"  # released at the end


def test_revision_only_once_and_audit_failure_does_not_break_the_mission(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(task_call("Revise: Research A"), report("A again"))
    llm.when(AUDIT, audit(("R1", "FAIL", [ISSUE]), ("R2", "PASS", None)), audit(("R1", "FAIL", [ISSUE])))
    llm.when(stage("CONSOLIDATION"), mission_report())
    store, _e, mid = run(registry, llm)
    titles = [x.title for x in store.snapshot().tasks]
    assert titles == ["Research A", "Check B", "Revise: Research A"]  # a failed revision is not revised again
    assert [a.final for a in store.audits_for(mid)] == [False, False, True]

    llm2 = FakeLLM()
    llm2.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm2.when(task_call("Research A"), report("A"))
    llm2.when(task_call("Check B"), report("B"))
    llm2.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm2.when(AUDIT, LLMError("overloaded"), LLMError("overloaded"))
    llm2.when(stage("CONSOLIDATION"), mission_report())
    store2, _e2, mid2 = run(registry, llm2)
    final = store2.snapshot().mission_reports[-1]
    assert store2.snapshot().missions[0].phase == "CLOSED" and not store2.audits_for(mid2)
    assert final.audit_summary.startswith("Audit skipped")


def test_audit_off_and_audit_revisions_zero(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(AUDIT, audit(("R1", "FAIL", [ISSUE]), ("R2", "PASS", None)))
    llm.when(stage("CONSOLIDATION"), mission_report())
    store, _e, mid = run(registry, llm, audit_revisions=0)
    assert len(store.snapshot().tasks) == 2 and store.audits_for(mid)[0].revision_task_id is None

    llm_off = FakeLLM()
    llm_off.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm_off.when(task_call("Research A"), report("A"))
    llm_off.when(task_call("Check B"), report("B"))
    llm_off.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm_off.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm_off)  # audit defaults to off in make_live
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store

    assert not calls_matching(llm_off, AUDIT) and not asyncio.run(go()).snapshot().audits


def test_precheck_floors_the_verdict():
    from atlas.core.models import AgentReport, Claim

    wrapped = AgentReport(mission_id="m", task_id="t", agent_id="sofia", asked_to="x",
                          findings=[Claim(kind="FACT", statement="Rent 18k", sources=[])],
                          limitations=["Auto-wrapped: the agent did not call submit_report",
                                       "Unverified: mentions data.xlsx but no file_read was recorded"])
    pre = precheck(wrapped)
    assert pre.floor == AuditVerdict.ISSUES
    assert any("cite no source" in x for x in pre.lines) and any("no system record" in x for x in pre.lines)
    assert any("No recorded actions" in x for x in pre.lines)
    assert isinstance(pre, Precheck)


def test_untraced_figures():
    from atlas.core.models import Claim, MissionReport

    rep = MissionReport(mission_id="m", objective_status="PARTIAL",
                        executive_summary="Rent averages $18,500 MXN; IRR 14% in the base case; 3 tasks ran in 2026.",
                        key_findings=[Claim(kind="RECOMMENDATION", statement="Buy below 4.2 MDP")])
    corpus = ["Average rent is 18500 MXN (source: listing)", "Base case IRR 14% (SCENARIO)"]
    out = untraced_figures(rep, corpus)
    assert len(out) == 1 and out[0].startswith("4.2") and "Buy below" in out[0]  # 3 and 2026 are ignored


def test_untraced_figures_reach_the_mission_report(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm.when(task_call("Research A"), report("Average rent is 18k MXN"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(AUDIT, audit(("R1", "PASS", None), ("R2", "PASS", None)))
    llm.when(stage("CONSOLIDATION"), mission_report("Rent is 18k MXN and the IRR is 23.4%."))
    store, _e, _mid = run(registry, llm)
    final = store.snapshot().mission_reports[-1]
    assert [u.split(" — ")[0] for u in final.untraced] == ["23.4%"]


# ---------------------------------------------------------------------------
# Retries, resume, usage
# ---------------------------------------------------------------------------


def test_transient_error_is_retried(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B", ["a"])))
    llm.when(task_call("Research A"), LLMError("OverloadedError: overloaded"), report("A"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm, task_retries=1, retry_delay=0)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store

    store = asyncio.run(go())
    task = store.snapshot().tasks[0]
    assert task.status == "COMPLETED" and task.retries == 1 and store.snapshot().tasks[1].status == "COMPLETED"
    assert any("retrying in 0s (1/1)" in e.summary for e in store.bus.history())


def test_resume_reruns_failed_and_cancelled_tasks(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B"),
                                     t("c", "oracle", "Analysis C", ["b"])))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Check B"), LLMError("boom"), report("B"))
    llm.when(task_call("Analysis C"), report("C"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report("v1", "PARTIAL"), mission_report("v2", "ACHIEVED"))

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("x", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 5)
        status = {x.title: x.status for x in store.snapshot().tasks}
        assert status == {"Research A": "COMPLETED", "Check B": "FAILED", "Analysis C": "CANCELLED"}
        await engine.resume(m.id)
        with pytest.raises(ValueError, match="running"):
            await engine.resume(m.id)
        await asyncio.wait_for(engine.wait(m.id), 5)
        with pytest.raises(ValueError, match="nothing to resume"):
            await engine.resume(m.id)
        return store, m.id

    store, _mid = asyncio.run(go())
    snap = store.snapshot()
    assert {x.title: x.status for x in snap.tasks} == {
        "Research A": "COMPLETED", "Check B": "COMPLETED", "Analysis C": "COMPLETED"}
    assert {x.title: x.round for x in snap.tasks} == {"Research A": 1, "Check B": 2, "Analysis C": 2}
    mission = snap.missions[0]
    assert mission.round == 2 and mission.phase == "CLOSED"
    assert [r.version for r in snap.mission_reports] == [1, 2]
    assert snap.mission_reports[-1].objective_status == "ACHIEVED"
    assert len(calls_matching(llm, task_call("Research A"))) == 1  # completed work is not redone


def test_usage_by_agent(registry):
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "sofia", "Research A"), t("b", "argos", "Check B")))
    llm.when(task_call("Research A"), report("A"))
    llm.when(task_call("Check B"), report("B"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(AUDIT, audit(("R1", "PASS", None), ("R2", "PASS", None)))
    llm.when(stage("CONSOLIDATION"), mission_report())
    store, _e, _mid = run(registry, llm)
    mission = store.snapshot().missions[0]
    by = mission.usage_by_agent
    assert by["atlas"].llm_calls == 3 and by["sofia"].llm_calls == 1 and by["argos"].llm_calls == 1
    assert by["auditor"].llm_calls == 1
    assert sum(u.llm_calls for u in by.values()) == mission.usage.llm_calls


def test_resume_and_usage_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_LOCAL_DIR", str(tmp_path))
    from atlas.main import app

    with TestClient(app) as client:
        r = client.post("/missions/msn_nope/resume")
        assert r.status_code == 404
        u = client.get("/usage?days=7").json()
        assert u["days"] == 7 and u["missions"] == 0 and u["totals"]["llm_calls"] == 0
        assert u["by_agent"] == {} and len(u["by_day"]) == 7 and "note" in u


def test_untraced_range_is_one_figure():
    from atlas.core.models import MissionReport

    rep = MissionReport(mission_id="m", objective_status="PARTIAL", executive_summary="Base-case IRR 15-18%.")
    assert [u.split(" — ")[0] for u in untraced_figures(rep, ["IRR between 15 and 17"])] == ["15-18%"]
    assert untraced_figures(rep, ["IRR 15% to 18%"]) == []
