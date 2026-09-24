"""Dev only: run the ATLAS API with the LIVE engine driven by a scripted fake model.

Exercises the whole live path (planning, parallel tasks, consult, approval, reports,
consolidation) through the real server and UI without an API key or cost.

    cd apps/api && uv run python ../../scripts/fake_live_server.py      # → http://localhost:8000
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-demo")  # makes /config report live_available

import uvicorn  # noqa: E402

from atlas.live.llm import call_text, text, tool_use  # noqa: E402
from atlas.main import app  # noqa: E402

PLAN = [
    {"ref": "research", "title": "Market comparables", "assigned_to": "sofia", "priority": "HIGH",
     "description": "Collect comparable projects, prices per m² and absorption."},
    {"ref": "check", "title": "Consistency check of developer figures", "assigned_to": "argos",
     "description": "Cross-check the developer's pro-forma against market data."},
    {"ref": "analysis", "title": "Return scenarios", "assigned_to": "oracle", "priority": "HIGH",
     "depends_on": ["research", "check"], "description": "Base/downside/upside IRR scenarios."},
    {"ref": "memo", "title": "Investment memo draft", "assigned_to": "alfred", "depends_on": ["analysis"],
     "requires_approval": True, "approval_reason": "EXTERNAL_COMMUNICATION",
     "description": "Draft the memo for the partners; ask approval before circulating."},
]


def _tools(kw: dict[str, Any]) -> set[str]:
    return {t.get("name") for t in kw.get("tools") or []}


def _had_tool_result(kw: dict[str, Any], name: str | None = None) -> bool:
    for m in kw.get("messages", []):
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and (name is None or b.get("name") == name):
                    return True
    return False


def _claim(kind: str, s: str, conf: str = "MEDIUM") -> dict[str, Any]:
    return {"kind": kind, "statement": s, "confidence": conf, "sources": ["demo"]}


def _report(summary: str) -> Any:
    return tool_use("submit_report", {
        "asked_to": summary, "actions_taken": ["Reviewed inputs", "Produced findings"],
        "inputs_used": ["Task brief", "Dependency reports"],
        "findings": [_claim("FACT", f"{summary}: key data point confirmed.", "HIGH"),
                     _claim("ASSUMPTION", "Market absorption stays near the 12-month average.")],
        "unresolved": ["Construction license date"], "confidence": "MEDIUM", "limitations": ["Demo model"],
    })


async def brain(**kw: Any) -> Any:
    await asyncio.sleep(random.uniform(1.0, 2.5))
    tools, prompt = _tools(kw), call_text(kw)
    if "create_plan" in tools:
        return tool_use("create_plan", {"rationale": "Research and verify in parallel, then analyze, then package.",
                                        "tasks": PLAN})
    if "request_followups" in tools:
        return tool_use("request_followups", {"assessment": "Reports are consistent enough.", "tasks": []})
    if "submit_mission_report" in tools:
        return tool_use("submit_mission_report", {
            "executive_summary": "Demo run: the opportunity is attractive only with a preferred return; "
                                 "developer absorption looks optimistic.",
            "objective_status": "PARTIAL",
            "key_findings": [_claim("FACT", "Prices are within the comparable range.", "HIGH"),
                             _claim("SCENARIO", "Base-case IRR 15-18%."),
                             _claim("RECOMMENDATION", "Negotiate a preferred return before committing.")],
            "conflicts": ["Developer absorption 2x market average"], "assumptions": ["Stable rates"],
            "needs_human_attention": ["Decide negotiation stance"], "next_actions": ["ALFRED: request term sheet"],
        })
    if "submit_report" not in tools:  # a consult answer
        return text("From my side: absorption for 2BR units is ~1.4/month across comparables.")
    agent = next((a for a in ("ORACLE", "ALFRED", "SOFIA", "ARGOS") if f"You are {a}" in prompt), "")
    if agent == "ORACLE" and not _had_tool_result(kw, "consult"):
        return tool_use("consult", {"agent_id": "sofia", "question": "Absorption by unit type?"},
                        say="Checking unit-type absorption with SOFIA.")
    if agent == "ALFRED" and not _had_tool_result(kw, "request_approval"):
        return tool_use("request_approval", {
            "reason": "EXTERNAL_COMMUNICATION", "title": "Send memo to partners",
            "detail": "The memo draft is ready. Approve circulating it to the 4 partners?",
            "proposed_action": "Email the memo PDF to the partners."})
    return _report(f"{agent.title() or 'Agent'} task")


class DemoLLM:
    async def create(self, **kw: Any) -> Any:
        return await brain(**kw)


_orig = app.router.lifespan_context


def _lifespan(a):
    class _Ctx:
        async def __aenter__(self):
            self.cm = _orig(a)
            await self.cm.__aenter__()
            a.state.live.llm = DemoLLM()
        async def __aexit__(self, *exc):
            return await self.cm.__aexit__(*exc)
    return _Ctx()


app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    uvicorn.run(app, port=int(os.getenv("PORT", "8000")))
