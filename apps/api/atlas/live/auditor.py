"""AUDITOR: the quality gate between the agents and the human (docs/AUDITOR.md).

For each agent report of a round, the system first runs deterministic checks (unsourced facts, file mentions
with no system record, auto-wrapped output, facts with no recorded action, self-reported external calls), then
AUDITOR reads the report next to the evidence ATLAS recorded for its task and returns a verdict:

    PASS    the report holds up
    ISSUES  usable, with the listed caveats
    FAIL    a key finding is unsupported / mislabeled / wrong → the orchestrator sends it back for ONE revision

After consolidation, `untraced_figures` checks the executive report deterministically: every figure in the
summary and key findings must appear in some agent report, the evidence or the human's own words.

AUDITOR never adds findings of its own and never writes to anything but its Audit records.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.models import AgentReport, Audit, AuditIssue, AuditVerdict, ClaimKind, Evidence, MissionReport
from .llm import LLMError, current_agent
from .prompts import render_report

log = logging.getLogger("atlas.live.auditor")

VERDICTS = [v.value for v in AuditVerdict]
ISSUE_KINDS = ["unsupported", "mislabeled", "inconsistent", "calculation", "stale", "scope", "other"]
SEVERITIES = ["LOW", "MEDIUM", "HIGH"]
MAX_EVIDENCE_LINES = 40
MAX_REPORT_CHARS = 6000

SUBMIT_AUDIT_TOOL: dict[str, Any] = {
    "name": "submit_audit",
    "description": "Submit your verdict on every report you were given (one entry per report ref).",
    "input_schema": {
        "type": "object",
        "properties": {
            "audits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "report_ref": {"type": "string", "description": "the ref of the report, e.g. R1"},
                        "verdict": {"type": "string", "enum": VERDICTS},
                        "summary": {"type": "string", "description": "one or two sentences"},
                        "issues": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "finding": {"type": "string", "description": "the questioned text, quoted"},
                                    "problem": {"type": "string",
                                                "description": "what is wrong; for FAIL, what the agent must fix"},
                                    "kind": {"type": "string", "enum": ISSUE_KINDS},
                                    "severity": {"type": "string", "enum": SEVERITIES},
                                },
                                "required": ["finding", "problem", "kind", "severity"],
                            },
                        },
                    },
                    "required": ["report_ref", "verdict", "summary", "issues"],
                },
            },
        },
        "required": ["audits"],
    },
}


# ---------------------------------------------------------------------------
# Deterministic checks (system, no LLM)
# ---------------------------------------------------------------------------


@dataclass
class Precheck:
    lines: list[str] = field(default_factory=list)
    floor: AuditVerdict = AuditVerdict.PASS  # the verdict can't be better than this


def _worse(a: AuditVerdict, b: AuditVerdict) -> AuditVerdict:
    order = {AuditVerdict.PASS: 0, AuditVerdict.ISSUES: 1, AuditVerdict.FAIL: 2}
    return a if order[a] >= order[b] else b


def precheck(report: AgentReport) -> Precheck:
    out = Precheck()
    facts = [c for c in report.findings if c.kind == ClaimKind.FACT.value]
    unsourced = [c for c in facts if not c.sources]
    if unsourced:
        out.lines.append(f"{len(unsourced)} of {len(facts)} FACT finding(s) cite no source")
    unverified = [x for x in report.limitations if x.lower().startswith("unverified")]
    if unverified:
        out.lines.append(f"Claim check: {len(unverified)} file/action mention(s) with no system record")
        out.floor = _worse(out.floor, AuditVerdict.ISSUES)
    if any("auto-wrapped" in x.lower() for x in report.limitations):
        out.lines.append("The agent never submitted a structured report; its raw output was wrapped")
        out.floor = _worse(out.floor, AuditVerdict.ISSUES)
    acted = [e for e in report.evidence if e.ok and e.kind != "approval"]
    if facts and not acted:
        out.lines.append("No recorded actions for this task: its facts rest only on the context and its inputs")
    if any(e.kind == "external_call" for e in report.evidence):
        out.lines.append("External agent: its actions are self-reported (ATLAS recorded only the call)")
    failed = [e for e in report.evidence if not e.ok]
    if failed:
        out.lines.append(f"{len(failed)} recorded action(s) failed (e.g. {failed[0].kind} {failed[0].ref})")
    if not out.lines:
        out.lines.append("No deterministic issue: every FACT cites a source and mentions match the evidence")
    return out


def evidence_lines(evidence: Iterable[Evidence]) -> list[str]:
    lines = []
    for e in evidence:
        status = "" if e.ok else " (FAILED)"
        detail = f" · {e.detail}" if e.detail else ""
        lines.append(f"- {e.kind} · {e.ref}{detail}{status}")
    if len(lines) > MAX_EVIDENCE_LINES:
        extra = len(lines) - MAX_EVIDENCE_LINES
        lines = lines[:MAX_EVIDENCE_LINES] + [f"- … {extra} more"]
    return lines or ["- (none: the agent recorded no actions)"]


# ---------------------------------------------------------------------------
# The audit step
# ---------------------------------------------------------------------------


@dataclass
class AuditItem:
    ref: str  # R1, R2… in the prompt
    report: AgentReport
    title: str
    agent_name: str
    pre: Precheck


def audit_message(objective: str, items: list[AuditItem], notes: list[str]) -> str:
    parts = ["STAGE: AUDIT", f"Mission objective:\n{objective}"]
    if notes:
        parts.append("Human guidance from the mission thread:\n- " + "\n- ".join(notes))
    for it in items:
        body = render_report(it.report, title=it.title, agent_name=it.agent_name)
        if len(body) > MAX_REPORT_CHARS:
            body = body[:MAX_REPORT_CHARS] + "\n… (truncated)"
        parts.append(
            f"=== {it.ref} ===\n{body}\n\nEvidence recorded by ATLAS for this task:\n"
            + "\n".join(evidence_lines(it.report.evidence))
            + "\n\nSystem checks already run:\n- " + "\n- ".join(it.pre.lines)
        )
    parts.append(f"Audit every report ({', '.join(i.ref for i in items)}) and call submit_audit now.")
    return "\n\n".join(parts)


def _issues(raw: Any) -> list[AuditIssue]:
    out = []
    for i in raw or []:
        if not isinstance(i, dict):
            continue
        kind = str(i.get("kind") or "other").lower()
        sev = str(i.get("severity") or "MEDIUM").upper()
        out.append(AuditIssue(
            finding=str(i.get("finding") or "").strip()[:500],
            problem=str(i.get("problem") or "").strip()[:800],
            kind=kind if kind in ISSUE_KINDS else "other",
            severity=sev if sev in SEVERITIES else "MEDIUM",
        ))
    return [i for i in out if i.problem]


def validate_audit(data: dict[str, Any] | None, refs: set[str]) -> list[str]:
    if data is None:
        return ["you must call submit_audit"]
    got = {str(a.get("report_ref") or "").strip() for a in data.get("audits") or [] if isinstance(a, dict)}
    errors = []
    missing = sorted(refs - got)
    if missing:
        errors.append(f"missing a verdict for: {', '.join(missing)}")
    unknown = sorted(got - refs)
    if unknown:
        errors.append(f"unknown report refs: {', '.join(unknown)}")
    for a in data.get("audits") or []:
        if isinstance(a, dict) and str(a.get("verdict") or "").upper() not in VERDICTS:
            errors.append(f"{a.get('report_ref')}: verdict must be one of {', '.join(VERDICTS)}")
    return errors


async def run_audit(live: Any, reports: list[AgentReport], *, final: bool) -> list[Audit]:
    """Audit `reports` in one AUDITOR step. Returns unsaved Audit objects (the orchestrator decides on revisions
    and records them). Raises LLMError when the step itself fails."""
    scope = live.scope
    auditor = scope.auditor
    store = scope.store
    items = []
    for i, r in enumerate(reports, start=1):
        task = store.task(r.task_id)
        items.append(AuditItem(f"R{i}", r, task.title, scope.name(r.agent_id), precheck(r)))
    refs = {it.ref for it in items}

    async def validate(data: dict[str, Any] | None) -> list[str]:
        return validate_audit(data, refs)

    token = current_agent.set(auditor.id)
    try:
        data = await live.executor.structured(
            scope, model=auditor.model, system=[auditor.role_prompt], prompt=audit_message(
                scope.objective, items, live._notes()),
            tool=SUBMIT_AUDIT_TOOL, max_tokens=scope.config.audit_max_tokens, validate=validate, attempts=2,
        )
    finally:
        current_agent.reset(token)
    if data is None:
        raise LLMError("AUDITOR did not call submit_audit")
    by_ref = {str(a.get("report_ref") or "").strip(): a for a in data.get("audits") or [] if isinstance(a, dict)}
    audits = []
    for it in items:
        raw = by_ref.get(it.ref)
        if raw is None:
            verdict, summary, issues = AuditVerdict.ISSUES, "AUDITOR returned no verdict for this report", []
        else:
            verdict = AuditVerdict(str(raw.get("verdict")).upper()) if str(raw.get("verdict") or "").upper() \
                in VERDICTS else AuditVerdict.ISSUES
            summary = str(raw.get("summary") or "").strip()[:600]
            issues = _issues(raw.get("issues"))
        verdict = _worse(verdict, it.pre.floor)
        if verdict == AuditVerdict.ISSUES and not issues and it.pre.floor == AuditVerdict.ISSUES:
            issues = [AuditIssue(finding="(system check)", problem=line, kind="unsupported", severity="MEDIUM")
                      for line in it.pre.lines[:3]]
        audits.append(Audit(
            mission_id=scope.mission_id, round=live.round, agent_report_id=it.report.id, task_id=it.report.task_id,
            agent_id=it.report.agent_id, verdict=verdict, summary=summary, issues=issues, checks=it.pre.lines,
            final=final,
        ))
    return audits


def revision_description(original: str, audit: Audit) -> str:
    issues = "\n".join(f"- [{i.severity} · {i.kind}] \"{i.finding}\": {i.problem}" for i in audit.issues) \
        or f"- {audit.summary}"
    return (
        f"{original}\n\n"
        "REVISION REQUESTED BY AUDITOR. Your previous report on this task is in your inputs. It did not hold up:\n"
        f"{issues}\n\n"
        "Fix each point: back facts with sources you actually open (read the file, fetch the page), relabel "
        "estimates as ASSUMPTION or SCENARIO, correct the arithmetic, and keep everything that already held up. "
        "If something cannot be verified, say so in limitations instead of asserting it."
    )


def audit_summary(audits: list[Audit], *, skipped: str | None = None) -> str:
    if skipped:
        return f"Audit skipped · {skipped}"
    if not audits:
        return ""
    first = [a for a in audits if not a.final]
    final = [a for a in audits if a.final]
    n = {v: sum(1 for a in first if a.verdict == v) for v in AuditVerdict}
    text = (f"AUDITOR checked {len(first)} report(s): {n[AuditVerdict.PASS]} held up, "
            f"{n[AuditVerdict.ISSUES]} with caveats, {n[AuditVerdict.FAIL]} did not hold up")
    sent = [a for a in first if a.revision_task_id]
    if sent:
        text += f"; {len(sent)} sent back for revision"
    if final:
        ok = sum(1 for a in final if a.verdict != AuditVerdict.FAIL)
        text += f"; after revision {ok} of {len(final)} held up"
    return text + "."


def audit_block(audits: list[Audit]) -> str:
    """The audit of one report, as ATLAS sees it at consolidation."""
    if not audits:
        return ""
    a = audits[-1]
    lines = [f"AUDIT: {a.verdict}" + (" (revision)" if a.final else "") + (f" — {a.summary}" if a.summary else "")]
    for i in a.issues[:6]:
        lines.append(f"  - [{i.severity} · {i.kind}] \"{i.finding}\": {i.problem}")
    if a.revision_task_id:
        lines.append("  → sent back for revision: use the revised report instead of this one")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The mission report: every figure must trace back
# ---------------------------------------------------------------------------

_RANGE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?[-–]\s?(\d[\d,]*(?:\.\d+)?)\s?%?")
_NUM = re.compile(r"(?<![\w.])[$€]?\s?\d[\d,]*(?:\.\d+)?\s?(?:%|k|K|M|MDP|mdp|MM|bn)?(?![\w])")


def _norm(token: str) -> str:
    t = token.replace(",", "").replace(" ", "").replace("$", "").replace("€", "")
    return t.lower()


def _figures(text: str) -> list[str]:
    out = []
    for m in _NUM.finditer(text or ""):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw.split(".")[0])
        bare = _norm(raw)
        if not digits:
            continue
        has_unit = bool(re.search(r"[%$€kKM]|mdp|bn", raw, re.IGNORECASE)) or "." in raw
        value = int(digits) if digits.isdigit() else 0
        if not has_unit and (value < 10 or 1900 <= value <= 2100):
            continue  # small counts and years are not figures worth tracing
        out.append(bare)
    return out


def untraced_figures(report: MissionReport, corpus: Iterable[str], limit: int = 10) -> list[str]:
    """Figures in the executive summary / key findings that appear in no agent report, evidence, objective or
    human note. Matching is on the normalized number (1,200 == 1200; 14% needs "14" in the corpus)."""
    haystack = " ".join(_norm(c) for c in corpus)
    plain = set(re.findall(r"\d+(?:\.\d+)?", haystack))
    texts = [report.executive_summary] + [c.statement for c in report.key_findings]
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        # a range ("15-18%") is one figure: untraced if either end is
        for m in _RANGE.finditer(text):
            ends = [re.sub(r"[^\d.]", "", e).rstrip(".") for e in (m.group(1), m.group(2))]
            label = m.group(0).strip()
            seen.update(ends)
            if all(e in plain for e in ends) or label in seen:
                continue
            seen.add(label)
            out.append(f"{label} — \"{_around(text, ends[0])}\"")
            if len(out) >= limit:
                return out
        for fig in _figures(text):
            number = re.sub(r"[^\d.]", "", fig).rstrip(".")
            if not number or number in seen:
                continue
            seen.add(number)
            if number in plain or fig in haystack:
                continue
            snippet = _around(text, number)
            out.append(f"{fig} — \"{snippet}\"")
            if len(out) >= limit:
                return out
    return out


def _around(text: str, number: str, width: int = 70) -> str:
    flat = text.replace(",", "")
    i = flat.find(number)
    if i < 0:
        return text[:width]
    start = max(0, i - width // 2)
    snippet = flat[start:start + width].strip()
    return ("…" if start else "") + snippet + ("…" if start + width < len(flat) else "")


def report_corpus(live: Any) -> list[str]:
    """Everything a figure in the executive report may legitimately come from."""
    s = live.store
    mid = live.mission_id
    out = [live.scope.objective, *live._notes()]
    for r in s.reports_for(mid):
        out += [c.statement for c in r.findings] + [x for c in r.findings for x in c.sources]
        out += r.actions_taken + r.inputs_used + r.unresolved + r.limitations
    out += [f"{e.ref} {e.detail}" for e in s.evidence_for(mid)]
    out += [m.body for m in s.messages_for(mid)]
    return out
