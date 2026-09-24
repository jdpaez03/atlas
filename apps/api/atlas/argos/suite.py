"""PAGA Suite client (docs/ARGOS.md § L10): read-only access to the L10 module.

    ATLAS_SUITE_URL          e.g. https://pagasuite.com/api
    ATLAS_SUITE_TOKEN        an API token (sent as `Bearer <token>` in Authorization)
    ATLAS_SUITE_AUTH_HEADER  optional header name (default Authorization; any other header gets the raw token)
    ATLAS_SUITE_TIMEOUT      optional seconds per request (default 20)

Endpoints: GET /l10/admin/todos, GET /l10/issues, GET /l10/resumen/{semana_id}.

The Suite's field names are not a published contract, so every record is mapped through alias lists
(`responsable`, `fecha`, `avance_pct`, `semaforo`, `estado`/`status`, `decide`, `abierto_desde`/`created_at`, …).
Unmapped field names are logged once per endpoint. ATLAS only reads; it never writes to the Suite.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .checks import CheckNotConfigured

log = logging.getLogger("atlas.argos.suite")

NOT_CONFIGURED_HINT = "Set ATLAS_SUITE_URL and ATLAS_SUITE_TOKEN in .env (a PAGA Suite API token)"
DEFAULT_TIMEOUT = 20.0

# -- aliases (normalized: lowercase, no accents, '_' separators) -----------------------------------------------

ID = ("id", "_id", "uuid", "todo_id", "issue_id", "id_todo", "id_issue", "clave", "folio")
TITLE = ("titulo", "title", "nombre", "name", "descripcion", "description", "texto", "text", "tarea", "todo",
         "issue", "tema", "asunto")
OWNER = ("responsable", "owner", "asignado", "asignado_a", "assignee", "assigned_to", "usuario", "user",
         "persona", "encargado", "responsable_nombre")
DUE = ("fecha", "fecha_limite", "fecha_compromiso", "fecha_entrega", "fecha_vencimiento", "vence", "due",
       "due_date", "deadline", "fecha_fin")
STATUS = ("estado", "status", "estatus", "state")
DONE_FLAG = ("completado", "completada", "terminado", "hecho", "done", "completed", "cerrado", "closed",
             "resuelto", "resolved")
PROGRESS = ("avance_pct", "avance", "porcentaje", "pct", "progress", "progress_pct", "percent")
SEMAFORO = ("semaforo", "color", "traffic_light", "light")
REPORTED = ("reportado", "reported", "reportado_semana", "has_report", "con_reporte")
LAST_REPORT = ("ultimo_reporte", "fecha_reporte", "last_report", "last_report_at", "reportado_en",
               "fecha_ultimo_reporte", "last_update", "ultima_actualizacion")
REPORTS = ("reportes", "reports", "historial", "updates", "avances")
WEEK = ("semana_id", "week_id", "semana", "week", "id_semana")
DECIDER = ("decide", "decisor", "decider", "quien_decide", "decision_maker", "responsable_decision")
OPENED = ("abierto_desde", "created_at", "creado", "fecha_alta", "fecha_creacion", "opened_at", "abierto",
          "fecha_apertura", "fecha")
PROJECT = ("proyecto", "project", "area", "equipo", "team")
WRAPPERS = ("data", "items", "results", "resultados", "rows", "registros", "todos", "issues", "records")

CLOSED_WORDS = {"done", "completado", "completada", "completo", "complete", "completed", "terminado",
                "terminada", "hecho", "hecha", "cerrado", "cerrada", "closed", "resuelto", "resuelta",
                "resolved", "solved", "finalizado", "finalizada", "cancelado", "cancelada", "cancelled",
                "canceled", "archivado", "archived"}
_KNOWN = set(ID + TITLE + OWNER + DUE + STATUS + DONE_FLAG + PROGRESS + SEMAFORO + REPORTED + LAST_REPORT
             + REPORTS + WEEK + DECIDER + OPENED + PROJECT)


class SuiteError(RuntimeError):
    """The Suite could not be read (unreachable, HTTP error, bad JSON). The message says which and why."""


def _norm_key(k: Any) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", str(k)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _pick(rec: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):  # {"nombre": "Ana", "email": ...}
        for k in ("nombre", "name", "nombre_completo", "full_name", "display_name", "email", "correo", "id"):
            if v.get(k):
                return str(v[k]).strip()
        return ""
    if isinstance(v, list):
        return " · ".join(t for t in (_text(x) for x in v) if t)
    return " ".join(str(v).split())


def parse_date(v: Any) -> date | None:
    """ISO dates/datetimes, dd/mm/yyyy, epoch seconds/millis. None when unparseable."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, int | float) and not isinstance(v, bool):
        ts = float(v) / (1000 if v > 1e11 else 1)
        try:
            return datetime.fromtimestamp(ts, UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    if m := re.match(r"^(\d{4})-(\d{2})-(\d{2})", s):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if m := re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s):  # Mexican day-first
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        s = _norm_key(v)
        if s in {"true", "si", "yes", "1", "y", "s"}:
            return True
        if s in {"false", "no", "0", "n"}:
            return False
    return None


def _float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def is_closed(status: str) -> bool:
    return _norm_key(status) in CLOSED_WORDS


@dataclass
class Todo:
    id: str
    title: str
    owner: str = ""
    due: date | None = None
    done: bool = False
    status: str = ""
    progress_pct: float | None = None
    semaforo: str = ""
    week_id: str = ""
    reported: bool | None = None  # explicit flag for the current week, when the Suite gives one
    last_report: date | None = None
    report_weeks: list[str] = field(default_factory=list)  # week ids with a report
    project: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Issue:
    id: str
    title: str
    owner: str = ""
    decider: str = ""
    opened: date | None = None
    status: str = ""
    closed: bool = False
    project: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _normalized(rec: dict[str, Any]) -> dict[str, Any]:
    return {_norm_key(k): v for k, v in rec.items()}


def map_todo(rec: dict[str, Any]) -> Todo | None:
    r = _normalized(rec)
    ident, title = _pick(r, ID), _text(_pick(r, TITLE))
    if ident is None and not title:
        return None
    status = _text(_pick(r, STATUS))
    flag = next((b for b in (_bool(r.get(k)) for k in DONE_FLAG) if b is not None), None)
    progress = _float(_pick(r, PROGRESS))
    if progress is not None and 0 < progress <= 1 and "pct" not in "".join(k for k in r if k in PROGRESS):
        progress *= 100  # a 0-1 fraction
    done = bool(flag) if flag is not None else (is_closed(status) if status else progress is not None
                                                 and progress >= 100)
    reports_raw = _pick(r, REPORTS)
    weeks: list[str] = []
    last = parse_date(_pick(r, LAST_REPORT))
    if isinstance(reports_raw, list):
        for rep in reports_raw:
            if isinstance(rep, dict):
                rr = _normalized(rep)
                if (w := _pick(rr, WEEK)) is not None:
                    weeks.append(str(w))
                d = parse_date(_pick(rr, ("fecha", "date", "created_at", "creado", "reportado_en")))
                if d and (last is None or d > last):
                    last = d
            elif rep not in (None, ""):
                weeks.append(str(rep))
    return Todo(
        id=str(ident if ident is not None else title), title=title or f"To-do {ident}",
        owner=_text(_pick(r, OWNER)), due=parse_date(_pick(r, DUE)), done=done, status=status,
        progress_pct=progress, semaforo=_text(_pick(r, SEMAFORO)), week_id=_text(_pick(r, WEEK)),
        reported=next((b for b in (_bool(r.get(k)) for k in REPORTED) if b is not None), None),
        last_report=last, report_weeks=weeks, project=_text(_pick(r, PROJECT)), raw=rec,
    )


def map_issue(rec: dict[str, Any]) -> Issue | None:
    r = _normalized(rec)
    ident, title = _pick(r, ID), _text(_pick(r, TITLE))
    if ident is None and not title:
        return None
    status = _text(_pick(r, STATUS))
    flag = next((b for b in (_bool(r.get(k)) for k in DONE_FLAG) if b is not None), None)
    return Issue(
        id=str(ident if ident is not None else title), title=title or f"Issue {ident}",
        owner=_text(_pick(r, OWNER + ("levantado_por", "reportado_por", "created_by"))),
        decider=_text(_pick(r, DECIDER)), opened=parse_date(_pick(r, OPENED)), status=status,
        closed=bool(flag) if flag is not None else is_closed(status), project=_text(_pick(r, PROJECT)), raw=rec,
    )


def unwrap(payload: Any, *prefer: str) -> list[dict[str, Any]]:
    """The list of records in a response: a bare list, or the first list under a known wrapper key."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        norm = _normalized(payload)
        for k in (*prefer, *WRAPPERS):
            v = norm.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):  # {"data": {"todos": [...]}}
                inner = unwrap(v, *prefer)
                if inner:
                    return inner
        lists = [v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if len(lists) == 1:
            return lists[0]
    return []


def envelope_week(payload: Any) -> str:
    """A current week id carried by the response wrapper ({"semana_id": 38, "todos": [...]}), if any."""
    if isinstance(payload, dict):
        norm = _normalized(payload)
        for k in ("semana_actual", "semana_abierta", "current_week", "open_week", *WEEK):
            v = norm.get(k)
            if isinstance(v, dict):
                v = _pick(_normalized(v), ID + WEEK)
            if v not in (None, "") and not isinstance(v, list | dict):
                return str(v)
    return ""


class SuiteClient:
    def __init__(self, base_url: str, token: str, *, auth_header: str = "Authorization",
                 timeout: float = DEFAULT_TIMEOUT, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.auth_header = auth_header or "Authorization"
        self.timeout = timeout
        self._transport = transport
        self._logged: set[str] = set()

    @classmethod
    def from_env(cls, transport: httpx.AsyncBaseTransport | None = None) -> SuiteClient:
        url = os.getenv("ATLAS_SUITE_URL", "").strip()
        token = os.getenv("ATLAS_SUITE_TOKEN", "").strip()
        if not url or not token:
            missing = " and ".join(n for n, v in (("ATLAS_SUITE_URL", url), ("ATLAS_SUITE_TOKEN", token)) if not v)
            raise CheckNotConfigured(f"PAGA Suite not configured ({missing} missing)", NOT_CONFIGURED_HINT)
        try:
            timeout = float(os.getenv("ATLAS_SUITE_TIMEOUT", "") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return cls(url, token, auth_header=os.getenv("ATLAS_SUITE_AUTH_HEADER", "").strip() or "Authorization",
                   timeout=timeout, transport=transport)

    def _headers(self) -> dict[str, str]:
        value = self.token
        if self.auth_header.lower() == "authorization" and not value.lower().startswith(("bearer ", "token ")):
            value = f"Bearer {value}"
        return {self.auth_header: value, "Accept": "application/json"}

    async def get(self, path: str) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        where = f"PAGA Suite GET /{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport,
                                         headers=self._headers(), follow_redirects=True) as client:
                resp = await client.get(url)
        except httpx.TimeoutException as exc:
            raise SuiteError(f"{where} timed out after {self.timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise SuiteError(f"{where} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SuiteError(f"{where} was refused (HTTP {resp.status_code}): check ATLAS_SUITE_TOKEN"
                             + ("" if self.auth_header.lower() == "authorization"
                                else f" and ATLAS_SUITE_AUTH_HEADER ({self.auth_header})"))
        if resp.status_code >= 400:
            body = " ".join(resp.text.split())[:200]
            raise SuiteError(f"{where} failed: HTTP {resp.status_code}" + (f" · {body}" if body else ""))
        try:
            return resp.json()
        except ValueError as exc:
            raise SuiteError(f"{where} did not return JSON (content-type "
                             f"{resp.headers.get('content-type', '?')})") from exc

    def _log_unknown(self, endpoint: str, records: list[dict[str, Any]]) -> None:
        if endpoint in self._logged or not records:
            return
        self._logged.add(endpoint)
        unknown = sorted({_norm_key(k) for r in records for k in r} - _KNOWN)
        if unknown:
            log.info("PAGA Suite %s: unmapped fields %s", endpoint, ", ".join(unknown))

    async def todos(self) -> tuple[list[Todo], str]:
        """(to-dos, current week id from the envelope or '')."""
        payload = await self.get("/l10/admin/todos")
        records = unwrap(payload, "todos")
        if not records and payload not in ([], {}, None):
            log.warning("PAGA Suite /l10/admin/todos: unrecognized response shape (%s)", type(payload).__name__)
        self._log_unknown("todos", records)
        out = [t for t in (map_todo(r) for r in records) if t is not None]
        if len(out) < len(records):
            log.warning("PAGA Suite /l10/admin/todos: skipped %d record(s) with no id or title",
                        len(records) - len(out))
        return out, envelope_week(payload)

    async def issues(self) -> list[Issue]:
        payload = await self.get("/l10/issues")
        records = unwrap(payload, "issues")
        if not records and payload not in ([], {}, None):
            log.warning("PAGA Suite /l10/issues: unrecognized response shape (%s)", type(payload).__name__)
        self._log_unknown("issues", records)
        return [i for i in (map_issue(r) for r in records) if i is not None]

    async def resumen(self, semana_id: str) -> dict[str, Any]:
        payload = await self.get(f"/l10/resumen/{semana_id}")
        return payload if isinstance(payload, dict) else {"items": payload}
