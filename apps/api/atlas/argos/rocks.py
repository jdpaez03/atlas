"""Rocks check (docs/ARGOS.md § Rocks): statuses and paces from PAGA Suite's Rocks module, or from the
user's own `rocks.yaml`.

Source (watch.yaml `rocks.source`: auto | suite | file; default auto): with PAGA Suite configured
(ATLAS_SUITE_URL + token or scope) the Suite is the source — it computes each Rock's status itself (pace rule,
owner's weekly declaration) and ARGOS maps it (see `from_suite`). Otherwise rocks.yaml, with the rules below.

Rules for rocks.yaml (the user's dossier):

Rules (the user's dossier):
  done: true                                   → DONE
  past due and not done                        → FAILED     (alert rock_failed, HIGH)
  no metric or no target                       → UNKNOWN    "needs a measurable" (alert other, LOW)
  observed pace < 50% of required pace         → AT_RISK    (alert rock_at_risk)
  otherwise                                    → ON_TRACK
  two owners ('·', '/', ',', '&', '+')         → alert other "Rock with two owners has no owner"

Paces use business weeks (5 weekdays) in America/Mexico_City:
  observed = (current − start_value) / weeks elapsed since start_date
  required = (target − current) / weeks left until due
A Rock whose target is below its start value (e.g. "days of delay: 30 → 5") counts progress downward.

Each Rock is emitted with `store.upsert_rock` (event rock.updated). `update_current` writes a new `current`
back to rocks.yaml, keeping the user's comments (ruamel.yaml round trip).
"""

from __future__ import annotations

import io
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.util import load_yaml_guess_indent

from ..core import paths
from ..core.models import AlertEvidence, RockStatus
from .checks import AlertDraft, CheckContext, CheckNotConfigured, CheckResult, Severity, fingerprint
from .l10 import local_today, record_evidence

log = logging.getLogger("atlas.argos.rocks")

AT_RISK_RATIO = 0.5
TWO_OWNERS = re.compile(r"\s*(?:·|/|,|&|\+)\s*")
_LOCK = threading.Lock()

EXAMPLE = """\
# ARGOS · Rocks (docs/ARGOS.md). Private: this file lives in ATLAS_LOCAL_DIR and is never committed.
#
# One entry per Rock. ARGOS computes each status on every check:
#   done: true                           → DONE
#   past `due` and not done              → FAILED (HIGH alert)
#   no `metric` or no `target`           → UNKNOWN, "needs a measurable" (LOW alert)
#   observed pace < 50% of required pace → AT_RISK
#       observed = (current - start_value) / business weeks since start_date
#       required = (target - current) / business weeks until due
#   otherwise                            → ON_TRACK
# One owner per Rock: "Ana / Luis" raises an alert (a Rock with two owners has no owner).
#
# Update `current` here or from the Monitor view (PATCH /rocks/{id}); comments are kept.
# Delete the `example: true` line once these are your real Rocks: until then the check is not configured.
example: true

quarter: 2026-Q4          # start_date defaults to the first day of the quarter

rocks:
  - id: R1                          # short, stable id
    title: Close 40 unit sales
    owner: Owner A                  # one person
    project: Project X              # optional, groups alerts
    due: 2026-12-31
    metric: units sold              # what you count
    start_value: 0                  # value at start_date (default 0)
    target: 40
    current: 12
    # start_date: 2026-10-01        # optional; default: first day of `quarter`
    done: false

  - id: R2
    title: Launch the new supplier portal
    owner: Owner B
    due: 2026-11-30
    # no metric/target yet → UNKNOWN until you give it a measurable
    done: false
"""


def rocks_path() -> Path:
    return paths.local_dir() / "argos" / "rocks.yaml"


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    return y


def ensure_example(path: Path | None = None) -> bool:
    """Write the commented example if the file is missing. True when it was written."""
    path = path or rocks_path()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(EXAMPLE)
    except FileExistsError:
        return False
    return True


class RocksFileError(ValueError):
    """rocks.yaml is unreadable or malformed (a ValueError: PATCH /rocks/{id} answers 422)."""


def _load(path: Path) -> CommentedMap:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RocksFileError(f"cannot read {path.name}: {exc}") from exc
    try:
        data = _yaml().load(text)
    except Exception as exc:
        raise RocksFileError(f"{path.name} is not valid YAML: {exc}") from exc
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise RocksFileError(f"{path.name} must be a mapping with `quarter` and `rocks`")
    return data


# -- parsing helpers ----------------------------------------------------------------------------------------


def _date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def quarter_bounds(quarter: str) -> tuple[date, date] | None:
    m = re.match(r"^\s*(\d{4})\s*[-_ ]?\s*Q([1-4])\s*$", str(quarter or ""), re.IGNORECASE) or \
        re.match(r"^\s*Q([1-4])\s*[-_ ]?\s*(\d{4})\s*$", str(quarter or ""), re.IGNORECASE)
    if not m:
        return None
    year, q = (int(m[1]), int(m[2])) if len(m[1]) == 4 else (int(m[2]), int(m[1]))
    start = date(year, 3 * (q - 1) + 1, 1)
    end = (date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)) - timedelta(days=1)
    return start, end


def business_weeks(start: date, end: date) -> float:
    """Weekdays strictly after `start` up to and including `end`, in weeks of 5 (negative spans → 0)."""
    from ..inbox.state import business_days_between

    return business_days_between(start, end) / 5.0


def two_owners(owner: str) -> bool:
    parts = [p for p in TWO_OWNERS.split(owner or "") if p.strip()]
    return len(parts) > 1


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _fmt(x: float | None) -> str:
    return "?" if x is None else f"{x:g}" if abs(x) >= 10 or x == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")


@dataclass
class Evaluation:
    rocks: list[RockStatus] = field(default_factory=list)
    alerts: list[AlertDraft] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _quote(lines: list[str], entry: Any, n: int = 300) -> str:
    """The Rock's own lines from rocks.yaml, verbatim (comments included)."""
    try:
        start = entry.lc.line
        first = lines[start]
    except (AttributeError, IndexError):
        return ""
    indent = len(first) - len(first.lstrip())
    block = [first.strip()]
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line.strip())
    text = " | ".join(block)
    return text if len(text) <= n else text[: n - 1] + "…"


Finding = tuple[str, str, Severity, str, str]  # (kind, fingerprint key, severity, title, detail)


def evaluate_rock(raw: dict[str, Any], *, quarter: str, today: date) -> tuple[RockStatus, list[Finding]]:
    """One Rock → (status, findings). Raises ValueError on a bad entry."""
    rid = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    due = _date(raw.get("due"))
    if not rid or not title or due is None:
        missing = [n for n, v in (("id", rid), ("title", title), ("due", due)) if not v]
        raise ValueError(f"missing {', '.join(missing)}")
    owner = " ".join(str(raw.get("owner") or "").split())
    metric = str(raw.get("metric") or "").strip() or None
    target, current = _num(raw.get("target")), _num(raw.get("current"))
    start_value = _num(raw.get("start_value"))
    bounds = quarter_bounds(quarter)
    start_date = _date(raw.get("start_date")) or (bounds[0] if bounds else None)
    done = bool(raw.get("done")) if not isinstance(raw.get("done"), str) else \
        str(raw.get("done")).strip().lower() in {"true", "yes", "si", "sí", "1", "done"}
    base: dict[str, Any] = {
        "id": rid, "title": title, "owner": owner or "—",
        "project": str(raw["project"]).strip() if raw.get("project") else None, "quarter": str(quarter or ""),
        "due": due, "metric": metric, "target": target, "current": current, "start_value": start_value,
        "start_date": start_date,
    }
    findings: list[Finding] = []
    if not owner:
        findings.append(("other", "owner", "LOW", f"Rock without an owner · {title}", "Every Rock needs exactly one owner."))
    elif two_owners(owner):
        findings.append(("other", "owner", "MEDIUM", f"Rock with two owners has no owner · {title}",
                         f"Owners: {owner}. Pick one: none of the shared-owner Rocks in the dossier closed."))

    if done:
        return RockStatus(**base, status="DONE", reason="Marked done"), findings
    if today > due:
        reason = f"Past due {due.isoformat()} and not done"
        if metric and target is not None:
            reason += f" ({metric}: {_fmt(current)} of {_fmt(target)})"
        findings.append(("rock_failed", "rock_failed", "HIGH", f"Rock failed · {title}", f"{owner or 'No owner'} · {reason}"))
        return RockStatus(**base, status="FAILED", reason=reason), findings
    if not metric or target is None:
        findings.append(("other", "measurable", "LOW", f"Rock needs a measurable · {title}",
                         (f"{owner or 'No owner'} · no metric/target, so progress can't be tracked (EOS: every "
                          "Rock needs a measurable)")))
        return RockStatus(**base, status="UNKNOWN", reason="needs a measurable"), findings
    if current is None:
        return RockStatus(**base, status="UNKNOWN", reason=f"no current value for {metric} yet"), findings

    sv = start_value if start_value is not None else 0.0
    direction = -1.0 if target < sv else 1.0
    remaining = (target - current) * direction
    weeks_left = business_weeks(today, due)
    if remaining <= 0:
        return RockStatus(**base, status="ON_TRACK", reason=f"Target reached ({_fmt(current)} of {_fmt(target)})",
                          required_pace=0.0,
                          observed_pace=_r(((current - sv) * direction) / w) if start_date and
                          (w := business_weeks(start_date, today)) > 0 else None), findings
    required = remaining / max(weeks_left, 0.2)  # due today → one business day left
    elapsed = business_weeks(start_date, today) if start_date else 0.0
    if elapsed <= 0:
        return RockStatus(**base, status="ON_TRACK", reason="Too early to measure pace",
                          required_pace=_r(required)), findings
    observed = ((current - sv) * direction) / elapsed
    ratio = observed / required if required > 0 else 1.0
    unit = f" {metric}/wk"
    if ratio < AT_RISK_RATIO:
        reason = (f"Observed pace {_fmt(observed)}{unit} is {max(ratio, 0) * 100:.0f}% of the required "
                  f"{_fmt(required)}{unit} ({_fmt(current)} of {_fmt(target)}, {weeks_left:g} weeks left)")
        sev: Severity = "HIGH" if weeks_left <= 2 else "MEDIUM"
        findings.append(("rock_at_risk", "rock_at_risk", sev, f"Rock at risk · {title}", f"{owner or 'No owner'} · {reason}"))
        status = "AT_RISK"
    else:
        reason = (f"Observed pace {_fmt(observed)}{unit} vs required {_fmt(required)}{unit} "
                  f"({_fmt(current)} of {_fmt(target)})")
        status = "ON_TRACK"
    return RockStatus(**base, status=status, reason=reason, required_pace=_r(required),
                      observed_pace=_r(observed)), findings


def evaluate(now: datetime, path: Path | None = None, *, write_example: bool = True) -> Evaluation:
    """Load rocks.yaml and compute every Rock. Raises CheckNotConfigured (missing/example file) or
    RocksFileError (unreadable)."""
    path = path or rocks_path()
    if write_example and ensure_example(path):
        raise CheckNotConfigured("rocks.yaml was missing; an example was written",
                                 f"Edit {path}: replace the example Rocks with yours and delete `example: true`")
    if not path.exists():
        raise CheckNotConfigured("rocks.yaml not found", f"Create {path} (see docs/ARGOS.md § Rocks)")
    data = _load(path)
    if data.get("example") is True:
        raise CheckNotConfigured("rocks.yaml is still the example",
                                 f"Edit {path}: replace the example Rocks with yours and delete `example: true`")
    today = local_today(now)
    quarter = str(data.get("quarter") or "")
    lines = path.read_text(encoding="utf-8").splitlines()
    out = Evaluation()
    entries = data.get("rocks") or []
    if not isinstance(entries, list):
        raise RocksFileError(f"{path.name}: `rocks` must be a list")
    if quarter and quarter_bounds(quarter) is None:
        out.notes.append(f"quarter '{quarter}' is not like 2026-Q4; start dates must be given per Rock")
    seen: set[str] = set()
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            out.notes.append(f"Rock #{n} skipped: not a mapping")
            continue
        try:
            rock, findings = evaluate_rock(entry, quarter=quarter, today=today)
        except ValueError as exc:
            out.notes.append(f"Rock #{n} skipped: {exc}")
            continue
        if rock.id in seen:
            out.notes.append(f"Rock #{n} skipped: duplicate id {rock.id}")
            continue
        seen.add(rock.id)
        out.rocks.append(rock)
        quote = _quote(lines, entry)
        ev = [AlertEvidence(source="rocks.yaml", quote=quote)] if quote else []
        for kind, key, sev, title, detail in findings:
            out.alerts.append(AlertDraft(check="rocks", kind=kind, severity=sev, title=title, detail=detail,
                                         fingerprint=fingerprint("rocks", key, rock.id), project=rock.project,
                                         evidence=list(ev)))
    return out


def summary_line(rocks: list[RockStatus]) -> str:
    on = sum(1 for r in rocks if r.status in ("ON_TRACK", "DONE"))
    by: dict[str, int] = {}
    for r in rocks:
        by[r.status] = by.get(r.status, 0) + 1
    parts = [f"{on} of {len(rocks)} on-track"] + [f"{v} {k.lower().replace('_', ' ')}" for k, v in sorted(by.items())
                                                  if k != "ON_TRACK"]
    return " · ".join(parts)


def load_rocks(now: datetime | None = None, path: Path | None = None) -> list[RockStatus]:
    """Compute every Rock (for POST /rocks/reload); raises like `evaluate`."""
    return evaluate(now or datetime.now().astimezone(), path).rocks


def update_current(rock_id: str, value: float, *, now: datetime | None = None,
                   path: Path | None = None) -> RockStatus:
    """Set `current` for one Rock in rocks.yaml, keeping comments and layout; returns its new status.
    Raises KeyError (unknown id or no file), ValueError (not a number; RocksFileError: bad YAML)."""
    path = path or rocks_path()
    if not path.exists():
        raise KeyError(f"{rock_id} (no rocks.yaml at {path})")
    num = _num(value)
    if num is None:
        raise ValueError("current must be a number")
    with _LOCK:
        data = _load(path)
        entry = next((e for e in (data.get("rocks") or []) if isinstance(e, dict)
                      and str(e.get("id") or "").strip() == str(rock_id)), None)
        if entry is None:
            raise KeyError(rock_id)
        entry["current"] = int(num) if num == int(num) else num
        y = _yaml()
        _, indent, offset = load_yaml_guess_indent(path.read_text(encoding="utf-8"))
        if indent:
            offset = offset or 0
            y.indent(mapping=max(2, indent - offset) if offset else indent, sequence=indent, offset=offset)
        buf = io.StringIO()
        y.dump(data, buf)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        tmp.replace(path)
    ev = evaluate(now or datetime.now().astimezone(), path, write_example=False)
    rock = next((r for r in ev.rocks if r.id == str(rock_id)), None)
    if rock is None:  # the entry is invalid (e.g. no due date); report it as such
        raise RocksFileError(f"Rock {rock_id} is not valid: " + "; ".join(ev.notes))
    return rock


async def emit(store: Any, rocks: list[RockStatus], mission_id: str | None = None, *, force: bool = False) -> int:
    """Upsert each new or changed Rock (rock.updated; `force` emits all). Returns how many were emitted
    (0 if the store lacks upsert_rock)."""
    upsert = getattr(store, "upsert_rock", None)
    if upsert is None:
        log.warning("store.upsert_rock is not available yet; %d Rock(s) not emitted", len(rocks))
        return 0
    known = {r.id: r for r in (store.rocks() if hasattr(store, "rocks") else [])}
    n = 0
    for rock in rocks:
        old = known.get(rock.id)
        if not force and old is not None and old.model_dump(exclude={"updated_at"}) == \
                rock.model_dump(exclude={"updated_at"}):
            continue
        await upsert(rock, mission_id=mission_id, agent_id="argos")
        n += 1
    return n


# ── PAGA Suite as the source ─────────────────────────────────────────────────────────────────────────────

SUITE_SOURCE = "PAGA Suite /rocks"
SUITE_STATUS = {"on_track": "ON_TRACK", "off_track": "OFF_TRACK", "sin_registro": "UNKNOWN",
                "por_declarar": "FAILED", "cumplido": "DONE", "no_cumplido": "FAILED"}


def suite_configured() -> bool:
    import os

    return bool(os.getenv("ATLAS_SUITE_URL", "").strip()
                and (os.getenv("ATLAS_SUITE_TOKEN", "").strip() or os.getenv("ATLAS_SUITE_SCOPE", "").strip()))


def rocks_source(config: dict[str, Any] | None) -> str:
    """'suite' or 'file', from watch.yaml `rocks.source` (auto → the Suite when it is configured)."""
    section = (config or {}).get("rocks") if isinstance((config or {}).get("rocks"), dict) else {}
    wanted = str((section or {}).get("source") or "auto").strip().lower()
    if wanted in ("suite", "file"):
        return wanted
    return "suite" if suite_configured() else "file"


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def from_suite(payload: dict[str, Any]) -> Evaluation:
    """The Suite's board → RockStatus + alerts. The Suite already decided each status; ARGOS only translates it
    and quotes the Suite's own reason as evidence."""
    out = Evaluation()
    trimestre = str(payload.get("trimestre") or "")
    for r in payload.get("rocks") or []:
        if not isinstance(r, dict) or not r.get("codigo"):
            continue
        estado = str(r.get("estado") or "")
        fuente = str(r.get("estado_fuente") or "")
        motivo = str(r.get("estado_motivo") or "")
        ritmo = r.get("ritmo") or {}
        due = _date(r.get("fecha_compromiso"))
        if due is None:
            out.notes.append(f"{r.get('codigo')}: no fecha_compromiso in the Suite's answer; skipped")
            continue
        rid = f"{r.get('trimestre') or trimestre}-{r['codigo']}"
        owner = str(r.get("responsable_nombre") or r.get("responsable_email") or "—")
        title = str(r.get("titulo") or rid)
        rock = RockStatus(
            id=rid, title=title, owner=owner, project=r.get("proyecto"), quarter=str(r.get("trimestre") or trimestre),
            due=due, metric=r.get("metrica"), target=_f(r.get("meta")), current=_f(r.get("valor_actual")),
            start_value=_f(r.get("valor_inicial")), start_date=_date(r.get("fecha_inicio")),
            status=SUITE_STATUS.get(estado, "UNKNOWN"), reason=motivo,
            required_pace=_f(ritmo.get("requerido")), observed_pace=_f(ritmo.get("observado")),
        )
        out.rocks.append(rock)
        quote = f"{r['codigo']} · {estado} ({fuente}) · {motivo}"[:300]
        ev = [AlertEvidence(source=SUITE_SOURCE, quote=quote)]
        weeks_left = _f(r.get("semanas_restantes")) or 0.0
        if estado == "off_track":
            automatic = fuente == "automatico"
            sev: Severity = "HIGH" if automatic or weeks_left <= 2 else "MEDIUM"
            what = "off-track by the pace rule (owner said on-track)" if automatic else "off-track"
            out.alerts.append(AlertDraft(
                check="rocks", kind="rock_at_risk", severity=sev, title=f"Rock {what} · {title}",
                detail=f"{owner} · {motivo}", project=r.get("proyecto"), evidence=ev,
                fingerprint=fingerprint("rocks", "rock_at_risk", rid)))
        elif estado == "por_declarar":
            out.alerts.append(AlertDraft(
                check="rocks", kind="rock_failed", severity="HIGH", title=f"Rock past due, not declared · {title}",
                detail=f"{owner} · due {due.isoformat()} — declare it cumplido, fallido or redefinido",
                project=r.get("proyecto"), evidence=ev, fingerprint=fingerprint("rocks", "rock_failed", rid)))
        elif estado == "sin_registro":
            out.alerts.append(AlertDraft(
                check="rocks", kind="other", severity="LOW", title=f"Rock without this week's update · {title}",
                detail=f"{owner} · {motivo}", project=r.get("proyecto"), evidence=ev,
                fingerprint=fingerprint("rocks", "no_update", rid)))
    if not out.rocks:
        out.notes.append(f"PAGA Suite has no Rocks for {trimestre or 'this quarter'} yet")
    return out


class RocksCheck:
    name = "rocks"

    def __init__(self, path: Path | None = None):
        self.path = path

    def preflight(self, config: dict[str, Any]) -> None:
        """Raises CheckNotConfigured when rocks.yaml is missing (the example is written) or still the example;
        with the Suite as the source, when the Suite isn't configured."""
        if self.path is None and rocks_source(config) == "suite":
            from .suite import SuiteClient

            SuiteClient.from_env()  # raises CheckNotConfigured with the setup hint
            return
        path = self.path or rocks_path()
        if ensure_example(path):
            raise CheckNotConfigured("rocks.yaml was missing; an example was written",
                                     f"Edit {path}: replace the example Rocks with yours and delete `example: true`")
        try:
            if _load(path).get("example") is True:
                raise CheckNotConfigured("rocks.yaml is still the example",
                                         f"Edit {path}: replace the example Rocks with yours and delete "
                                         "`example: true`")
        except RocksFileError:
            return  # the run reports the parse error

    async def run(self, ctx: CheckContext) -> CheckResult:
        if self.path is None and rocks_source(ctx.config) == "suite":
            from .suite import SuiteClient

            client = SuiteClient.from_env()
            payload = await client.rocks()
            await record_evidence(ctx, "web_fetch", f"{client.base_url}/rocks",
                                  f"{len(payload.get('rocks') or [])} Rocks · {payload.get('trimestre', '')}")
            ev = from_suite(payload)
            await emit(ctx.store, ev.rocks, ctx.mission_id)
            return CheckResult(alerts=ev.alerts, notes=[f"Source: PAGA Suite · {payload.get('trimestre', '')}",
                                                        summary_line(ev.rocks), *ev.notes])
        path = self.path or rocks_path()
        ev = evaluate(ctx.now, path)
        await record_evidence(ctx, "file_read", str(path), f"{len(ev.rocks)} Rocks")
        await emit(ctx.store, ev.rocks, ctx.mission_id)
        return CheckResult(alerts=ev.alerts, notes=[summary_line(ev.rocks), *ev.notes])


try:
    from .engine import register_check

    register_check("rocks", RocksCheck)
except ImportError:  # pragma: no cover — engine not present yet
    pass
