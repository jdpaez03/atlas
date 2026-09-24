"""L10 check (docs/ARGOS.md § L10): deterministic findings from the PAGA Suite, no LLM.

  overdue_todo     past its date and not done                     severity by days overdue (≥7 MEDIUM, ≥14 HIGH)
  unreported_todo  not done and no report for the current week    LOW (MEDIUM when the last report is ≥14 days old)
  stale_issue      open > l10.stale_issue_days (default 14)       MEDIUM (> 2× HIGH); no decider only: LOW

Fingerprints are `l10:<kind>:<id>`. ATLAS only reads the Suite: it never reports on behalf of others.

watch.yaml (optional):
    l10:
      stale_issue_days: 14
      semana_id: 38          # the Suite's current week id, when the API doesn't say it
      use_resumen: true      # read /l10/resumen/{semana_id} for its list of unreported to-dos, if it has one
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from ..core.models import AlertEvidence
from .checks import AlertDraft, CheckContext, CheckResult, Severity, fingerprint
from .suite import Issue, SuiteClient, SuiteError, Todo, _norm_key, _text

log = logging.getLogger("atlas.argos.l10")

TODOS_SOURCE = "PAGA Suite /l10/admin/todos"
ISSUES_SOURCE = "PAGA Suite /l10/issues"
UNREPORTED_KEYS = ("sin_reporte", "no_reportados", "pendientes_de_reporte", "pendientes_reporte", "unreported",
                   "faltantes", "missing_reports", "todos_sin_reporte")


def local_today(now: datetime) -> date:
    from ..inbox.state import aware, user_tz

    return aware(now).astimezone(user_tz()).date()


def quote_of(raw: dict[str, Any], n: int = 300) -> str:
    text = json.dumps(raw, ensure_ascii=False, default=str, separators=(", ", ": "))
    return text if len(text) <= n else text[: n - 1] + "…"


def _cfg(ctx: CheckContext) -> dict[str, Any]:
    raw = (ctx.config or {}).get("l10") if isinstance(ctx.config, dict) else None
    return raw if isinstance(raw, dict) else {}


def _int(v: Any, default: int) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


async def record_evidence(ctx: CheckContext, kind: str, ref: str, detail: str = "", ok: bool = True) -> None:
    """Evidence on the ARGOS run mission (skipped in pure unit tests with no mission)."""
    if not ctx.mission_id:
        return
    from ..live.evidence import record

    try:
        ctx.store.mission(ctx.mission_id)
        await record(ctx.store, mission_id=ctx.mission_id, task_id=getattr(ctx, "task_id", None),
                     agent_id="argos", kind=kind, ref=ref, detail=detail, ok=ok)
    except Exception as exc:  # noqa: BLE001 — evidence never breaks a check
        log.debug("evidence not recorded: %s", exc)


def _sev_overdue(days: int) -> Severity:
    return "HIGH" if days >= 14 else "MEDIUM" if days >= 7 else "LOW"


def _unreported_ids(resumen: dict[str, Any]) -> set[str] | None:
    norm = {_norm_key(k): v for k, v in resumen.items()}
    for k in UNREPORTED_KEYS:
        v = norm.get(k)
        if isinstance(v, list):
            out: set[str] = set()
            for item in v:
                if isinstance(item, dict):
                    n = {_norm_key(a): b for a, b in item.items()}
                    ident = next((n[x] for x in ("id", "todo_id", "id_todo", "_id", "uuid") if n.get(x)), None)
                    if ident is not None:
                        out.add(str(ident))
                elif item not in (None, ""):
                    out.add(str(item))
            return out
    return None


def is_unreported(todo: Todo, week_id: str, week_start: date, listed: set[str] | None) -> bool | None:
    """True/False, or None when the Suite gives nothing to decide with."""
    if listed is not None:
        return todo.id in listed
    if todo.reported is not None and (not todo.week_id or not week_id or todo.week_id == week_id):
        return not todo.reported
    if todo.report_weeks and week_id:
        return week_id not in todo.report_weeks
    if todo.last_report is not None:
        return todo.last_report < week_start
    return None


class L10Check:
    name = "l10"

    def __init__(self, client: SuiteClient | None = None):
        self._client = client

    def preflight(self, config: dict[str, Any]) -> None:
        """Raises CheckNotConfigured when the Suite URL/token are missing (the engine answers 409 with the hint)."""
        self.client()

    def client(self) -> SuiteClient:
        return self._client or SuiteClient.from_env()  # raises CheckNotConfigured

    async def run(self, ctx: CheckContext) -> CheckResult:
        client = self.client()
        cfg = _cfg(ctx)
        stale_days = _int(cfg.get("stale_issue_days"), 14)
        today = local_today(ctx.now)
        week_start = today - timedelta(days=today.weekday())

        try:
            todos, env_week = await client.todos()
        except SuiteError as exc:
            await record_evidence(ctx, "web_fetch", f"{client.base_url}/l10/admin/todos", str(exc), ok=False)
            raise
        await record_evidence(ctx, "web_fetch", f"{client.base_url}/l10/admin/todos", f"{len(todos)} to-dos")
        try:
            issues = await client.issues()
        except SuiteError as exc:
            await record_evidence(ctx, "web_fetch", f"{client.base_url}/l10/issues", str(exc), ok=False)
            raise
        await record_evidence(ctx, "web_fetch", f"{client.base_url}/l10/issues", f"{len(issues)} issues")

        week_id = _text(cfg.get("semana_id")) or env_week
        notes: list[str] = []
        listed: set[str] | None = None
        if week_id and cfg.get("use_resumen", True) is not False:
            try:
                listed = _unreported_ids(await client.resumen(week_id))
            except SuiteError as exc:
                notes.append(f"Weekly summary not read ({exc}); report status taken from the to-dos")

        result = CheckResult(notes=notes)
        result.alerts += self.todo_alerts(todos, today, week_id, week_start, listed, notes)
        result.alerts += self.issue_alerts(issues, today, stale_days)
        counts: dict[str, int] = {}
        for a in result.alerts:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        open_todos = sum(1 for t in todos if not t.done)
        open_issues = sum(1 for i in issues if not i.closed)
        notes.insert(0, f"{len(todos)} to-dos ({open_todos} open) · {counts.get('overdue_todo', 0)} overdue · "
                        f"{counts.get('unreported_todo', 0)} unreported · {open_issues} open issues "
                        f"({counts.get('stale_issue', 0)} stale or without a decider)")
        return result

    @staticmethod
    def todo_alerts(todos: list[Todo], today: date, week_id: str, week_start: date,
                    listed: set[str] | None, notes: list[str]) -> list[AlertDraft]:
        out: list[AlertDraft] = []
        unknown = 0
        for t in todos:
            if t.done:
                continue
            who = t.owner or "no owner"
            ev = [AlertEvidence(source=TODOS_SOURCE, quote=quote_of(t.raw))]
            project = t.project or None
            if t.due and t.due < today:
                days = (today - t.due).days
                out.append(AlertDraft(
                    check="l10", kind="overdue_todo", severity=_sev_overdue(days),
                    title=f"Overdue to-do · {t.title}",
                    detail=f"{who} · due {t.due.isoformat()} ({days} day{'s' if days != 1 else ''} ago)"
                           + (f" · {t.progress_pct:g}% done" if t.progress_pct is not None else "")
                           + (f" · {t.semaforo}" if t.semaforo else ""),
                    fingerprint=fingerprint("l10", "overdue_todo", t.id), project=project, evidence=ev,
                ))
            state = is_unreported(t, week_id, week_start, listed)
            if state is None:
                unknown += 1
            elif state:
                stale = t.last_report is not None and (today - t.last_report).days >= 14
                last = f"last report {t.last_report.isoformat()}" if t.last_report else "no report on record"
                out.append(AlertDraft(
                    check="l10", kind="unreported_todo", severity="MEDIUM" if stale else "LOW",
                    title=f"Unreported to-do · {t.title}",
                    detail=f"{who} · no report for week {week_id or week_start.isoformat()} · {last}",
                    fingerprint=fingerprint("l10", "unreported_todo", t.id), project=project, evidence=ev,
                ))
        if unknown:
            notes.append(f"Report status unknown for {unknown} open to-do{'s' if unknown != 1 else ''} "
                         "(the Suite gave no report fields)")
        return out

    @staticmethod
    def issue_alerts(issues: list[Issue], today: date, stale_days: int) -> list[AlertDraft]:
        out: list[AlertDraft] = []
        for i in issues:
            if i.closed:
                continue
            age = (today - i.opened).days if i.opened else None
            old = age is not None and age > stale_days
            if not old and i.decider:
                continue
            reasons = []
            if old:
                reasons.append(f"open {age} days (limit {stale_days})")
            elif age is not None:
                reasons.append(f"open {age} days")
            if not i.decider:
                reasons.append("no decider")
            sev: Severity = "HIGH" if old and age is not None and age > 2 * stale_days else "MEDIUM" if old else "LOW"
            out.append(AlertDraft(
                check="l10", kind="stale_issue", severity=sev, title=f"Stale issue · {i.title}",
                detail=" · ".join([*(p for p in (i.owner,) if p), *reasons,
                                   *([f"decider {i.decider}"] if i.decider else [])]),
                fingerprint=fingerprint("l10", "stale_issue", i.id), project=i.project or None,
                evidence=[AlertEvidence(source=ISSUES_SOURCE, quote=quote_of(i.raw))],
            ))
        return out


try:
    from .engine import register_check

    register_check("l10", L10Check)
except ImportError:  # pragma: no cover — engine not present yet
    pass
