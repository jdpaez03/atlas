"""Weekly L10 brief (docs/ARGOS.md § Weekly brief).

Every number is computed here, from the store (alerts, Rocks, follow-ups) and rocks.yaml. ARGOS only writes the
prose, through one structured step (`write_brief`), and a validator rejects any number that isn't in the facts it
was given; if the step fails twice (or there is no LLM) the brief falls back to the computed lines.

Output: a `Brief` (event brief.ready through `store.upsert_brief`) and `L10 brief <ISO week>.docx` in
`<ATLAS_LOCAL_DIR>/outputs/<node>/argos/`, served by `GET /briefs/{id}/file` (see `brief_file`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..core import paths
from ..core.models import Alert, Attachment, Brief, FollowUp, RockStatus
from ..live.files import render_deliverable
from .checks import CheckContext, CheckNotConfigured
from .l10 import local_today, record_evidence
from .rocks import RocksFileError, evaluate, summary_line

log = logging.getLogger("atlas.argos.brief")

SECTIONS = ("rocks", "dashboards", "l10", "followups")
SECTION_TITLES = {"rocks": "Rocks", "dashboards": "Dashboards", "l10": "L10", "followups": "Follow-ups"}
L10_KINDS = {"overdue_todo": "overdue to-dos", "unreported_todo": "unreported to-dos", "stale_issue": "stale issues"}
MINE = ("MY_COMMITMENT", "REQUEST_TO_ME")
SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

WRITE_BRIEF_TOOL: dict[str, Any] = {
    "name": "write_brief",
    "description": "Write the Monday L10 brief from the computed facts. Use only numbers that appear in the facts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                         "description": "≤ 3 short lines: what matters most this week"},
            "sections": {
                "type": "object",
                "properties": {s: {"type": "array", "items": {"type": "string"}} for s in SECTIONS},
                "required": list(SECTIONS),
            },
        },
        "required": ["headline", "sections"],
    },
}

BRIEF_NOTE = """STAGE: ARGOS BRIEF
You write the user's Monday L10 brief (EOS Level 10 meeting). You receive FACTS computed by code.
- Use ONLY numbers that appear in the facts; never compute, round or invent a number.
- headline: at most 3 short lines, the most important things to act on in the meeting.
- sections.rocks: the first line must be the facts' "X of N on-track" line, then one line per Rock at risk or
  failed with its reason. sections.dashboards: open alerts by project. sections.l10: overdue and unreported to-dos,
  stale issues. sections.followups: the user's overdue items. Each line short, concrete, with the owner when known.
- An empty area gets one line saying so (e.g. "No open dashboard alerts").
- Write in the language of the facts' item titles (Spanish if they are in Spanish). Call write_brief once."""


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def brief_dir(node: str = "corporate") -> Path:
    return paths.local_dir() / "outputs" / paths.safe_name(node) / "argos"


def brief_file(brief: Brief) -> Path | None:
    """The docx behind a Brief (for GET /briefs/{id}/file); None if missing or outside the briefs folder."""
    if brief.deliverable is None or not brief.deliverable.name:
        return None
    root = brief_dir(brief.node)
    target = root / paths.safe_name(brief.deliverable.name)
    return target if target.is_file() and paths.is_within(target, root) else None


# ---------------------------------------------------------------------------
# Facts (all numbers computed here)
# ---------------------------------------------------------------------------


@dataclass
class BriefFacts:
    week: str
    today: date
    rocks: list[RockStatus] = field(default_factory=list)
    lines: dict[str, list[str]] = field(default_factory=dict)  # computed lines per section
    headline: list[str] = field(default_factory=list)  # computed fallback headline
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        out = [f"ISO week: {self.week} · today {self.today.isoformat()}"]
        for s in SECTIONS:
            out.append(f"\n## {SECTION_TITLES[s]}")
            out += [f"- {x}" for x in self.lines.get(s, [])]
        return "\n".join(out)


def _rocks_for(ctx: CheckContext, rocks_path: Path | None) -> tuple[list[RockStatus], str | None]:
    try:
        return evaluate(ctx.now, rocks_path, write_example=False).rocks, None
    except (CheckNotConfigured, RocksFileError) as exc:
        stored = list(ctx.store.snapshot().rocks)
        return stored, f"Rocks from the last check ({exc})"


def _alerts(ctx: CheckContext) -> list[Alert]:
    return [a for a in ctx.store.snapshot().alerts if a.status != "RESOLVED" and a.node == ctx.node]


def _sorted(alerts: list[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (SEV_ORDER.get(a.severity, 3), a.title))


def _alert_line(a: Alert) -> str:
    ack = " (acknowledged)" if a.status == "ACKNOWLEDGED" else ""
    return f"[{a.severity}] {a.title}" + (f" — {a.detail}" if a.detail else "") + ack


def gather(ctx: CheckContext, rocks_path: Path | None = None) -> BriefFacts:
    today = local_today(ctx.now)
    facts = BriefFacts(week=iso_week(today), today=today)
    L = facts.lines

    # Rocks
    rocks, note = _rocks_for(ctx, rocks_path)
    if note:
        facts.notes.append(note)
    facts.rocks = rocks
    n = len(rocks)
    on = sum(1 for r in rocks if r.status in ("ON_TRACK", "DONE"))
    bad = [r for r in rocks if r.status in ("AT_RISK", "OFF_TRACK", "FAILED")]
    unknown = [r for r in rocks if r.status == "UNKNOWN"]
    facts.counts.update(rocks=n, rocks_on_track=on, rocks_at_risk=sum(r.status == "AT_RISK" for r in rocks),
                        rocks_failed=sum(r.status == "FAILED" for r in rocks), rocks_unknown=len(unknown))
    L["rocks"] = [summary_line(rocks) if rocks else "0 of 0 on-track (no Rocks loaded)"]
    L["rocks"] += [f"{r.status.replace('_', ' ')} · {r.title} ({r.owner}) — {r.reason}" for r in
                   sorted(bad, key=lambda r: (r.status != "FAILED", r.due))]
    L["rocks"] += [f"UNKNOWN · {r.title} ({r.owner}) — {r.reason}" for r in unknown]

    alerts = _alerts(ctx)
    # Dashboards: open alerts by project
    dash = [a for a in alerts if a.check == "dashboards"]
    facts.counts["dashboard_alerts"] = len(dash)
    by_project: dict[str, list[Alert]] = {}
    for a in dash:
        by_project.setdefault(a.project or "(no project)", []).append(a)
    L["dashboards"] = []
    for project in sorted(by_project):
        items = _sorted(by_project[project])
        high = sum(a.severity == "HIGH" for a in items)
        L["dashboards"].append(f"{project}: {len(items)} open alert{'s' if len(items) != 1 else ''}"
                               + (f" ({high} HIGH)" if high else ""))
        L["dashboards"] += [f"  {_alert_line(a)}" for a in items]
    if not dash:
        L["dashboards"] = [_all_clear_or_why("dashboards", "No open dashboard alerts")]

    # L10
    l10 = [a for a in alerts if a.check == "l10"]
    L["l10"] = []
    for kind, label in L10_KINDS.items():
        items = _sorted([a for a in l10 if a.kind == kind])
        facts.counts[kind] = len(items)
        if items:
            L["l10"].append(f"{len(items)} {label}")
            L["l10"] += [f"  {_alert_line(a)}" for a in items]
    if not L["l10"]:
        L["l10"] = [_all_clear_or_why("l10", "No overdue or unreported to-dos, no stale issues")]

    # Follow-ups: the user's overdue items from the inbox board
    ups: list[FollowUp] = [f for f in ctx.store.followups(node=ctx.node) if f.status in ("OPEN", "WAITING")
                           and f.due is not None and f.due < today]
    mine = sorted([f for f in ups if f.kind in MINE], key=lambda f: f.due or today)
    theirs = [f for f in ups if f.kind not in MINE]
    facts.counts.update(followups_overdue=len(mine), followups_waiting_overdue=len(theirs))
    L["followups"] = [f"{len(mine)} overdue item{'s' if len(mine) != 1 else ''} of yours"]
    for f in mine:
        days = (today - f.due).days if f.due else 0
        L["followups"].append(f"  {f.title}" + (f" · {f.counterpart}" if f.counterpart else "")
                              + f" · due {f.due.isoformat() if f.due else '?'} ({days} days late)")
    if theirs:
        L["followups"].append(f"{len(theirs)} overdue item{'s' if len(theirs) != 1 else ''} you are waiting on")

    # Fallback headline (computed)
    head = [f"Rocks: {summary_line(rocks)}" if rocks else "Rocks: none loaded"]
    high = [a for a in alerts if a.severity == "HIGH"]
    if high:
        head.append(f"{len(high)} HIGH alert{'s' if len(high) != 1 else ''}: {_sorted(high)[0].title}")
    l10_total = sum(facts.counts.get(k, 0) for k in L10_KINDS)
    if l10_total or mine:
        head.append(f"L10: {facts.counts.get('overdue_todo', 0)} overdue to-dos, "
                    f"{facts.counts.get('stale_issue', 0)} stale issues · {len(mine)} follow-ups of yours overdue")
    facts.headline = head[:3]
    return facts


# ---------------------------------------------------------------------------
# Prose (ARGOS, structured) with a number guard
# ---------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in _NUM.findall(text):
        v = m.replace(",", ".")
        out.add(v.lstrip("0") or "0")
        for part in re.split(r"[.,]", m):  # "12.5" also allows "12" and "5"; "2026-09-30" → 2026, 9, 30
            out.add(part.lstrip("0") or "0")
    return out


def _lines(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [" ".join(str(x).split()) for x in v if str(x).strip()]


def check_prose(data: dict[str, Any] | None, facts: BriefFacts) -> list[str]:
    if data is None:
        return ["call write_brief"]
    errors: list[str] = []
    head = _lines(data.get("headline"))
    if not head:
        errors.append("headline needs 1-3 lines")
    if len(head) > 3:
        errors.append("headline must have at most 3 lines")
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    for s in SECTIONS:
        if not _lines(sections.get(s)):
            errors.append(f"sections.{s} needs at least one line")
    rocks = _lines(sections.get("rocks"))
    on_line = f"{facts.counts.get('rocks_on_track', 0)} of {facts.counts.get('rocks', 0)}"
    if rocks and on_line not in rocks[0]:
        errors.append(f"sections.rocks must start with the line containing '{on_line} on-track'")
    allowed = _numbers(facts.text())
    written = " ".join(head + [x for s in SECTIONS for x in _lines(sections.get(s))])
    extra = sorted({x for x in _numbers(written) if x not in allowed})
    if extra:
        errors.append("these numbers are not in the facts: " + ", ".join(extra[:12])
                      + " (use only the computed numbers)")
    return errors


def _executor(ctx: CheckContext, given: Any) -> Any:
    ex = given or getattr(ctx, "executor", None)
    if ex is not None:
        return ex
    scope = ctx.scope
    if scope is not None and scope.meter.llm is not None:
        from ..live.executor import ApiExecutor

        return ApiExecutor()
    from ..live.sdk import SubscriptionExecutor

    return SubscriptionExecutor()


async def write_prose(ctx: CheckContext, facts: BriefFacts, executor: Any = None
                      ) -> tuple[list[str], dict[str, list[str]], str | None]:
    """(headline, sections, note). Falls back to the computed lines (with a note) when ARGOS can't write it."""
    fallback = (facts.headline, {s: list(facts.lines.get(s, [])) for s in SECTIONS})
    scope = ctx.scope
    if scope is None:
        return (*fallback, "no LLM step available; computed lines used")
    agent = scope.agents.get("argos") or scope.orchestrator
    prompt = (f"Write the L10 brief for {facts.week}.\n\nFACTS (computed by code; every number you use must "
              f"come from here)\n{facts.text()}")

    async def validate(data: dict[str, Any] | None) -> list[str]:
        return check_prose(data, facts)

    try:
        data = await _executor(ctx, executor).structured(
            scope, model=agent.model, system=[agent.role_prompt, BRIEF_NOTE], prompt=prompt,
            tool=WRITE_BRIEF_TOOL, max_tokens=min(scope.config.max_tokens, 4000), validate=validate, attempts=2,
        )
    except Exception as exc:  # noqa: BLE001 — LLMError, SDK errors: the brief still ships with computed lines
        log.warning("ARGOS brief prose failed: %s", exc)
        return (*fallback, f"ARGOS could not write the prose ({exc}); computed lines used")
    if data is None:
        return (*fallback, "ARGOS's prose failed validation twice; computed lines used")
    sections = data["sections"]
    return _lines(data["headline"])[:3], {s: _lines(sections.get(s)) for s in SECTIONS}, None


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def _cell(v: Any) -> str:
    return str(v).replace("|", "/").strip() if v is not None else ""


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:g}"


def brief_markdown(facts: BriefFacts, headline: list[str], sections: dict[str, list[str]]) -> str:
    out = [f"# L10 brief {facts.week}", "", f"Prepared by ARGOS on {facts.today.isoformat()}.", ""]
    out += [f"- **{h}**" for h in headline] + [""]
    for s in SECTIONS:
        out += [f"## {SECTION_TITLES[s]}", ""]
        for line in sections.get(s, []):
            out.append(f"- {line.strip()}")
        out.append("")
        if s == "rocks" and facts.rocks:
            out.append("| Rock | Owner | Status | Current / Target | Pace req. / obs. (per wk) | Due |")
            out.append("|---|---|---|---|---|---|")
            for r in facts.rocks:
                metric = f"{_num(r.current)} / {_num(r.target)}" + (f" {r.metric}" if r.metric else "")
                out.append(f"| {_cell(r.title)} | {_cell(r.owner)} | {r.status.replace('_', ' ')} | {_cell(metric)} "
                           f"| {_num(r.required_pace)} / {_num(r.observed_pace)} | {r.due.isoformat()} |")
            out.append("")
    if facts.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in facts.notes] + [""]
    return "\n".join(out)


def write_docx(node: str, week: str, markdown: str) -> tuple[str, Path, int]:
    """Write `L10 brief <week>.docx` (never overwriting: ' (2)', ' (3)'…). Returns (name, path, size)."""
    data = render_deliverable("docx", markdown, None)
    out = brief_dir(node)
    out.mkdir(parents=True, exist_ok=True)
    stem = paths.safe_name(f"L10 brief {week}")
    name, n = f"{stem}.docx", 2
    while True:
        target = out / name
        try:
            with open(target, "xb") as fh:
                fh.write(data)
            return name, target, len(data)
        except FileExistsError:
            name = f"{stem} ({n}).docx"
            n += 1
            if n > 999:
                raise


async def build_brief(ctx: CheckContext, *, executor: Any = None, rocks_path: Path | None = None) -> Brief:
    """Compute the facts, have ARGOS write the prose, write the docx, emit brief.ready. Returns the Brief."""
    facts = gather(ctx, rocks_path)
    headline, sections, note = await write_prose(ctx, facts, executor)
    if note:
        facts.notes.append(note)
    brief = Brief(node=ctx.node, week=facts.week, headline=headline, sections=sections, mission_id=ctx.mission_id)
    name, target, size = write_docx(ctx.node, facts.week, brief_markdown(facts, headline, sections))
    brief.deliverable = Attachment(name=name, kind="file", size_bytes=size,
                                   download_url=f"/briefs/{brief.id}/file")
    await record_evidence(ctx, "file_written", str(target), f"{size:,} bytes")
    upsert = getattr(ctx.store, "upsert_brief", None)
    if upsert is not None:
        brief = await upsert(brief, mission_id=ctx.mission_id, agent_id="argos") or brief
    else:
        log.warning("store.upsert_brief is not available yet; brief %s not emitted", brief.id)
    return brief



def _all_clear_or_why(check: str, all_clear: str) -> str:
    """Never report "all clear" for a check that didn't actually run successfully (lead).

    An empty section only means "nothing found" when the check last ran OK; otherwise say why it's empty."""
    import json

    from ..core import paths

    try:
        runs = json.loads((paths.local_dir() / "argos" / "state.json").read_text(encoding="utf-8")).get("runs", {})
        run = (runs.get("checks") or {}).get(check) or {}
    except (OSError, ValueError, AttributeError):
        run = {}
    state = run.get("state")
    if state == "ok":
        return all_clear
    if state == "not configured":
        return f"Not checked: {run.get('hint') or 'not configured'}"
    if state in ("failed", "partial"):
        return f"Check incomplete ({state}) — last note: {(run.get('note') or '')[:160]}"
    return "Not checked yet (no successful run)"
