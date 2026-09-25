"""Dev only: run the ATLAS API with the LIVE engine driven by a scripted fake model.

Exercises the whole live path (planning, parallel tasks, consult, approval, reports,
consolidation) through the real server and UI without an API key or cost.

    cd apps/api && uv run python ../../scripts/fake_live_server.py      # → http://localhost:8000
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from typing import Any

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-demo")  # makes /config report live_available

import uvicorn

from atlas.live.llm import call_text, text, tool_use
from atlas.main import app

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


def _latest_attachment() -> str | None:
    from atlas.core import paths
    files = sorted((paths.local_dir() / "missions").glob("*/attachments/*"), key=lambda f: f.stat().st_mtime)
    return str(files[-1]) if files else None


async def brain(**kw: Any) -> Any:
    await asyncio.sleep(random.uniform(1.0, 2.5))
    tools, prompt = _tools(kw), call_text(kw)
    if "record_dashboard_findings" in tools:
        prev = prompt.split("===== PREVIOUS VERSION =====", 1)[1].split("===== END PREVIOUS =====", 1)[0]
        cur = prompt.split("===== CURRENT VERSION =====", 1)[1].split("===== END CURRENT =====", 1)[0]
        pl = next((ln.strip() for ln in prev.splitlines() if "Escrituraciones" in ln), None)
        cl = next((ln.strip() for ln in cur.splitlines() if "Escrituraciones" in ln), None)
        gone = next((ln.strip() for ln in prev.splitlines() if "Agua y Drenaje" in ln and ln not in cur), None)
        findings = []
        if pl and cl and pl != cl:
            findings.append({"kind": "moved_date", "severity": "HIGH", "title": "Escrituraciones movida 30-sep → 30-oct",
                             "detail": "El hito del Rock salió del trimestre sin declararse.",
                             "evidence": [{"version": "previous", "quote": pl}, {"version": "current", "quote": cl}]})
        if gone:
            findings.append({"kind": "removed_row", "severity": "MEDIUM", "title": "Desapareció el renglón 'Contrato Agua y Drenaje'",
                             "evidence": [{"version": "previous", "quote": gone}]})
        findings.append({"kind": "value_change", "severity": "LOW", "title": "Cifra inventada (debe descartarse)",
                         "evidence": [{"version": "current", "quote": "Escrituradas 99/51"}]})
        return tool_use("record_dashboard_findings", {"findings": findings})
    if "write_brief" in tools:
        return text("(demo: no prose — the brief falls back to computed lines)")
    if "summarize_threads" in tools:
        convs = re.findall(r"##### conversation_id: (\S+)", prompt)
        threads = [{"conversation_id": c, "summary": ["Avance de obra reportado en 62%.", "Se acordó revisar el programa el lunes."],
                    "decisions": ["Revisar programa el lunes"], "figures": ["62%"],
                    "asks_me": "Confirmar si el Scorecard incluye el avance de obra" if i == 0 else None,
                    "importance": "HIGH" if i == 0 else "MEDIUM"} for i, c in enumerate(convs)]
        return tool_use("summarize_threads", {"headline": ["Amāra reporta 62% de avance; revisión del programa el lunes."],
                                              "threads": threads})
    if "record_followups" in tools:
        items = []
        for m in re.finditer(r"=== message_id: (\S+) \((SENT BY ME|RECEIVED)\) ===\n(.*?)=== end ===", prompt, re.DOTALL):
            mid, direction, block = m.groups()
            sender = re.search(r"From: (.*)", block).group(1)
            subject = re.search(r"Subject: (.*)", block).group(1)
            body = block.split("--- body ---", 1)[1].strip()
            first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0][:200]
            if direction == "SENT BY ME":
                items.append({"message_id": mid, "kind": "AWAITING_REPLY", "title": f"Respuesta a: {subject}",
                              "counterpart": re.search(r"To: (.*)", block).group(1), "excerpt": first})
            else:
                items.append({"message_id": mid, "kind": "THEIR_COMMITMENT", "title": subject,
                              "counterpart": sender, "due": "2026-09-21", "priority": "HIGH", "excerpt": first})
        return tool_use("record_followups", {"items": items})
    if "draft_email" in tools:
        fid = re.search(r"followup_id: (\S+)", prompt).group(1)
        who = (re.search(r"Counterpart: (.*)", prompt).group(1) or "").strip()
        title = re.search(r"Title: (.*)", prompt).group(1)
        return tool_use("draft_email", {"followup_id": fid, "to": [who], "subject": f"Seguimiento: {title}",
                                        "body": "Hola,\n\nTe escribo para dar seguimiento a lo que quedamos. "
                                                "¿Me confirmas el estatus y una fecha?\n\nGracias,\nJuan Diego"})
    if "acknowledge" in tools:
        return tool_use("acknowledge", {"text": "Noted — I'll pass this to the agents still working."})
    if "respond_to_followup" in tools:
        last = prompt.lower()
        if "profundiza" in last or "deeper" in last or "más detalle" in last:
            return tool_use("respond_to_followup", {
                "answer": "Opening a second round: ORACLE will stress-test the downside case.",
                "tasks": [{"ref": "stress", "title": "Downside stress test", "assigned_to": "oracle",
                           "priority": "HIGH", "description": "Run -15% price / +6 months absorption."}]})
        return tool_use("respond_to_followup", {
            "answer": "From round 1: base-case IRR is 15-18%; the main risk is the developer's absorption assumption."})
    if "submit_audit" in tools:  # AUDITOR: ARGOS's report fails once (revision), SOFIA's has caveats, rest pass
        entries = []
        for ref, who in re.findall(r"=== (R\d+) ===\n## Report · .*? · by (\w+)", prompt):
            if "## Report · Revise:" in prompt or who not in ("ARGOS", "SOFIA"):
                entries.append({"report_ref": ref, "verdict": "PASS", "summary": "Holds up against its evidence.",
                                "issues": []})
            elif who == "ARGOS":
                entries.append({"report_ref": ref, "verdict": "FAIL", "summary": "The key check cites no source.",
                                "issues": [{"finding": "Consistency check passed", "kind": "unsupported",
                                            "severity": "HIGH",
                                            "problem": "No document was opened: read the data room file you checked "
                                                       "and cite it."}]})
            else:
                entries.append({"report_ref": ref, "verdict": "ISSUES", "summary": "Usable; one estimate is labelled FACT.",
                                "issues": [{"finding": "absorption ~1.4/month", "kind": "mislabeled", "severity": "MEDIUM",
                                            "problem": "An average of comparables, not a measured fact: label it ASSUMPTION."}]})
        return tool_use("submit_audit", {"audits": entries})
    if "submit_documents" in tools:  # SCRIBE: the demo's report as institutional documents
        return tool_use("submit_documents", {
            "doc_kind": "Reporte ejecutivo", "title": "Oportunidad de *inversión*",
            "subtitle": "Demo · evaluación de la oportunidad",
            "summary": [("La oportunidad es atractiva solo con un retorno preferente; la absorción del "
                         "desarrollador luce optimista (2x el promedio del mercado)."),
                        "Se recomienda negociar un retorno preferente antes de comprometer capital."],
            "highlights": [{"label": "TIR escenario base", "value": "15-18%"},
                           {"label": "Absorción 2 recámaras", "value": "1.4", "note": "unidades/mes"}],
            "sections": [
                {"title": "Hallazgos", "blocks": [
                    {"type": "bullets", "items": ["Los precios están dentro del rango de comparables.",
                                                  "La absorción del desarrollador es 2x el promedio del mercado."]},
                    {"type": "table", "table": {"columns": ["Escenario", "TIR"],
                                                "rows": [["Base", "15-18%"], ["Bajista", "7-10%"]],
                                                "source": "Reportes de ORACLE y ALFRED"}}]},
                {"title": "Recomendación", "blocks": [
                    {"type": "callout", "title": "Siguiente paso", "text": "ALFRED: solicitar term sheet."}]}],
            "slides": [
                {"type": "statement", "text": "Atractiva solo con un *retorno preferente*."},
                {"type": "kpis", "title": "Cifras *clave*", "kpis": [{"label": "TIR base", "value": "15-18%"},
                                                                       {"label": "Absorción", "value": "1.4"}]},
                {"type": "table", "title": "Escenarios", "table": {"columns": ["Escenario", "TIR"],
                                                                    "rows": [["Base", "15-18%"], ["Bajista", "7-10%"]]}},
                {"type": "bullets", "title": "Siguientes *pasos*", "items": ["Negociar retorno preferente.",
                                                                             "Solicitar term sheet."]}],
            "sources": ["Reportes de los agentes de la misión"]})
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
    if agent == "SOFIA" and "read_file" in tools and not _had_tool_result(kw, "read_file"):
        path = _latest_attachment()
        if path:
            return tool_use("read_file", {"path": path}, say="Reading the attached file.")
    if agent == "ALFRED" and _had_tool_result(kw, "request_approval") and not _had_tool_result(kw, "write_deliverable"):
        return tool_use("write_deliverable", {
            "filename": "Investment memo.docx", "format": "docx",
            "content": "# Investment memo\n\n**Recommendation:** negotiate a preferred return.\n\n"
                       "- Base-case IRR 15-18%\n- Absorption risk: developer 2x market\n\n"
                       "| Scenario | IRR |\n|---|---|\n| Base | 15-18% |\n| Downside | 7-10% |"})
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
    uvicorn.run(app, port=int(os.getenv("PORT", "8000")), timeout_graceful_shutdown=3)
