"""ATLAS's stages share one cached prefix: same tool list, 1-hour system breakpoint, and the dossier that REVIEW
and CONSOLIDATION both read sent first with its own breakpoint. A one-task plan is valid."""

from __future__ import annotations

import asyncio

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

from atlas.core.registry import AgentRegistry
from atlas.live import FakeLLM
from atlas.live.llm import FakeUsage
from atlas.live.pricing import PriceTable
from atlas.live.prompts import ORCHESTRATOR, split_stage


def test_single_task_mission_and_shared_cached_prefix():
    registry = AgentRegistry.load()
    llm = FakeLLM()
    llm.when(stage("PLANNING"), plan(t("a", "alfred", "Junta a to-dos")))
    llm.when(task_call("Junta a to-dos"), report("3 to-dos"))
    llm.when(stage("REVIEW"), NO_FOLLOWUPS)
    llm.when(stage("CONSOLIDATION"), mission_report())

    async def go():
        store, engine = make_live(registry, llm)
        m = await engine.start("Lee la junta y da de alta los to-dos", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 5)
        return store, m.id

    store, mid = asyncio.run(go())
    assert [x.title for x in store.tasks_for(mid)] == ["Junta a to-dos"]
    steps = [calls_matching(llm, stage(n))[0] for n in ("PLANNING", "REVIEW", "CONSOLIDATION")]
    names = [[x["name"] for x in c["tools"]] for c in steps]
    assert names[0] == names[1] == names[2] == ["create_plan", "request_followups", "submit_mission_report",
                                                 "respond_to_followup"]
    assert [c["tool_choice"]["name"] for c in steps] == ["create_plan", "request_followups", "submit_mission_report"]
    assert all(c["system"] == steps[0]["system"] for c in steps)
    assert steps[0]["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert isinstance(steps[0]["messages"][0]["content"], str)  # planning: nothing to share
    review, consolidation = (c["messages"][0]["content"] for c in steps[1:])
    assert review[0] == consolidation[0] and review[0]["cache_control"] == {"type": "ephemeral"}
    assert "3 to-dos" in review[0]["text"] and review[1]["text"].startswith("STAGE: REVIEW")
    assert consolidation[1]["text"].startswith("STAGE: CONSOLIDATION")


def test_prompt_and_pricing_details():
    assert "plan ONE task" in ORCHESTRATOR and "merge them" in ORCHESTRATOR
    assert split_stage("STAGE: PLANNING\nx") == ("", "STAGE: PLANNING\nx")
    assert split_stage("dossier\n\nSTAGE: REVIEW\ny") == ("dossier", "STAGE: REVIEW\ny")
    p = PriceTable()
    five = p.estimate("claude-opus-x", cache_write_tokens=1_000_000)
    hour = p.estimate("claude-opus-x", cache_write_1h_tokens=1_000_000)
    assert five == 6.25 and hour == 10.0
    assert FakeUsage().cache_creation_input_tokens == 0
