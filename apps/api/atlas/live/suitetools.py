"""The agents' PAGA Suite tools: Level 10 to-dos, issues, Rocks (docs/SUITE_TOOLS.md).

    suite_read(what, ...)          read the L10 module: to-dos, issues, the meeting view, the week, Rocks
    suite_add_todos(todos, ...)    add NEW to-dos (never edits an existing one)       } each one asks the human
    suite_open_week(fecha_junta)   open the L10 week of a meeting date                } in ATLAS first and runs
    suite_close_week(semana_id)    close a week: freezes its reports                  } only if approved

Reads use the same connection as ARGOS (ATLAS_SUITE_URL + ATLAS_SUITE_TOKEN / ATLAS_SUITE_SCOPE). Writes also need
ATLAS_SUITE_WRITE=1 here and ATLAS_API_KEY_WRITE=1 in the Suite (paga-app api/main.py `_atlas_puede`): the Suite's
key then accepts exactly POST /l10/admin/importar (new to-dos only), POST /l10/admin/semanas and
POST /l10/admin/semanas/{id}/cerrar.

The approval is part of the write tool itself: the human sees the exact rows that will be written, and the tool
sends exactly those rows after the approval, so an agent can't write anything that wasn't approved.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

READ_WHAT = ("todos", "issues", "junta", "contexto", "resumen", "rocks")
MAX_READ_CHARS = 60_000
MAX_TODOS = 40
TODO_FIELDS = ("titulo", "responsable", "fecha_compromiso", "proyecto", "contexto", "decide", "involucrados")


def suite_configured() -> bool:
    from ..argos.suite_auth import configured_scope

    return bool(os.getenv("ATLAS_SUITE_URL", "").strip()
                and (os.getenv("ATLAS_SUITE_TOKEN", "").strip() or configured_scope()))


def write_enabled() -> bool:
    return os.getenv("ATLAS_SUITE_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


class SuiteToolError(Exception):
    """A refusal or failure the agent can read."""


# -- tool specs -------------------------------------------------------------------------------------------------

SUITE_READ_TOOL: dict[str, Any] = {
    "name": "suite_read",
    "description": (
        "Read PAGA Suite's Level 10 module (live data). what: 'todos' = every to-do (filter estado: 'vivos' for "
        "the open ones), 'issues' = the issues list (estado: 'abiertos'), 'junta' = the meeting view of a week "
        "(default the current one: each to-do's report, semáforo, who needs help), 'contexto' = the current "
        "week (its id, clave, estado abierta/cerrada, cierra_en) and the catalogs (projects, people), "
        "'resumen' = a week's summary (needs semana_id), 'rocks' = the quarter's Rocks with their status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "what": {"type": "string", "enum": list(READ_WHAT)},
            "semana_id": {"type": "integer", "description": "a week id (junta, resumen)"},
            "estado": {"type": "string", "description": "todos: 'vivos' or a state; issues: 'abiertos'"},
        },
        "required": ["what"],
    },
}

SUITE_ADD_TODOS_TOOL: dict[str, Any] = {
    "name": "suite_add_todos",
    "description": (
        "Add NEW to-dos to PAGA Suite's Level 10 list (e.g. the commitments of the weekly meeting). This tool "
        "asks the human for approval itself, showing these exact rows, and writes them only if approved: do NOT "
        "call request_approval for it. It can't edit or close an existing to-do: first suite_read 'todos' "
        "(estado 'vivos') and leave out anything already there. One row per commitment: titulo (a verb, what "
        "will be done), responsable (the person's name or email as in the Suite's people catalog; '' if the "
        "meeting didn't name one), fecha_compromiso (YYYY-MM-DD, or '' if none was said), proyecto (as in the "
        "Suite's catalog; '' = corporativo), contexto (one line: where it came from, e.g. the minute of the "
        "transcript). Never invent an owner or a date."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array", "maxItems": MAX_TODOS,
                "items": {
                    "type": "object",
                    "properties": {f: {"type": "string"} for f in TODO_FIELDS},
                    "required": ["titulo"],
                },
            },
            "fuente": {"type": "string", "description": "where these come from, e.g. 'Junta semanal 2026-09-28 "
                                                        "(transcripción)'"},
        },
        "required": ["todos", "fuente"],
    },
}

SUITE_OPEN_WEEK_TOOL: dict[str, Any] = {
    "name": "suite_open_week",
    "description": (
        "Open the Level 10 week of a meeting date in PAGA Suite (it creates the week's report rows for every "
        "live to-do). Asks the human for approval itself; don't call request_approval for it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"fecha_junta": {"type": "string", "description": "the meeting's date, YYYY-MM-DD"},
                       "motivo": {"type": "string"}},
        "required": ["fecha_junta"],
    },
}

SUITE_CLOSE_WEEK_TOOL: dict[str, Any] = {
    "name": "suite_close_week",
    "description": (
        "Close a Level 10 week in PAGA Suite: its reports freeze (afterwards they can only be corrected with an "
        "audited version). First suite_read 'junta' for that week and put in `resumen` what will be frozen: "
        "how many to-dos reported, who didn't report, open help requests. Asks the human for approval itself; "
        "don't call request_approval for it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"semana_id": {"type": "integer"},
                       "clave": {"type": "string", "description": "the week's key, e.g. 2026-S40"},
                       "resumen": {"type": "string", "description": "what the week leaves frozen"}},
        "required": ["semana_id", "resumen"],
    },
}

READ_TOOLS = [SUITE_READ_TOOL]
WRITE_TOOLS = [SUITE_ADD_TODOS_TOOL, SUITE_OPEN_WEEK_TOOL, SUITE_CLOSE_WEEK_TOOL]
NAMES = tuple(t["name"] for t in READ_TOOLS + WRITE_TOOLS)
WRITE_NAMES = tuple(t["name"] for t in WRITE_TOOLS)


def tools() -> list[dict[str, Any]]:
    """The Suite tools this deployment offers (none when the Suite isn't configured)."""
    if not suite_configured():
        return []
    return READ_TOOLS + (WRITE_TOOLS if write_enabled() else [])


def note() -> str:
    """For the task message and the planner."""
    if not suite_configured():
        return ""
    text = ("PAGA Suite (Level 10): suite_read gives live to-dos, issues, the meeting view, the current week and "
            "Rocks.")
    if write_enabled():
        text += (" suite_add_todos adds new to-dos, suite_open_week / suite_close_week open and close the week: "
                 "each asks the human for approval inside the tool (no separate request_approval).")
    else:
        text += " Writing to the Suite is off in this deployment (ATLAS_SUITE_WRITE)."
    return text


# -- the calls --------------------------------------------------------------------------------------------------

@dataclass
class WritePlan:
    """What a write tool will send, and how the approval shows it."""

    path: str
    body: Any
    title: str
    detail: str
    proposed_action: str
    ref: str


def _client():
    from ..argos.checks import CheckNotConfigured
    from ..argos.suite import SuiteClient

    try:
        return SuiteClient.from_env()
    except CheckNotConfigured as exc:
        raise SuiteToolError(str(exc)) from exc


def _compact(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"… [truncated: {len(text) - MAX_READ_CHARS} more characters; filter with estado]"
    return text


async def read(data: dict[str, Any]) -> tuple[str, str]:
    """(text for the agent, evidence ref)."""
    from ..argos.checks import CheckNotConfigured
    from ..argos.suite import SuiteError

    what = str(data.get("what") or "").strip().lower()
    if what not in READ_WHAT:
        raise SuiteToolError(f"what must be one of {', '.join(READ_WHAT)}")
    semana = data.get("semana_id")
    estado = str(data.get("estado") or "").strip()
    if what == "resumen":
        if not semana:
            raise SuiteToolError("resumen needs semana_id (suite_read 'contexto' gives the current week's id)")
        path = f"/l10/resumen/{int(semana)}"
    elif what == "todos":
        path = "/l10/admin/todos" + (f"?estado={estado}" if estado else "")
    elif what == "issues":
        path = "/l10/issues" + (f"?estado={estado}" if estado else "")
    elif what == "junta":
        path = "/l10/junta" + (f"?semana_id={int(semana)}" if semana else "")
    else:
        path = f"/{what}" if what == "rocks" else f"/l10/{what}"
    try:
        payload = await _client().get(path)
    except (SuiteError, CheckNotConfigured) as exc:
        raise SuiteToolError(str(exc)) from exc
    return f"PAGA Suite GET {path}:\n{_compact(payload)}", path


def _clean_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise SuiteToolError(f"fecha_compromiso '{raw}' is not a date (YYYY-MM-DD, or '' if none was said)") from exc


def plan_write(name: str, data: dict[str, Any]) -> WritePlan:
    """Validate a write and describe it for the approval. Raises SuiteToolError on bad input."""
    if not write_enabled():
        raise SuiteToolError("Writing to PAGA Suite is off (ATLAS_SUITE_WRITE=1 in ATLAS's .env turns it on)")
    if name == "suite_add_todos":
        rows = data.get("todos")
        if not isinstance(rows, list) or not rows:
            raise SuiteToolError("todos must be a non-empty list")
        if len(rows) > MAX_TODOS:
            raise SuiteToolError(f"at most {MAX_TODOS} to-dos per call")
        items, lines = [], []
        for n, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise SuiteToolError(f"to-do {n} is not an object")
            item = {f: " ".join(str(row.get(f) or "").split()) for f in TODO_FIELDS}
            if not item["titulo"]:
                raise SuiteToolError(f"to-do {n} has no titulo")
            item["fecha_compromiso"] = _clean_date(item["fecha_compromiso"])
            item["contexto"] = item["contexto"][:500]
            item = {k: v for k, v in item.items() if v}
            item["origen"] = "junta"
            items.append(item)
            lines.append(f"{n}. {item['titulo']} · responsable: {item.get('responsable') or '(sin dueño)'} · "
                         f"fecha: {item.get('fecha_compromiso') or '(sin fecha)'}"
                         + (f" · proyecto: {item['proyecto']}" if item.get("proyecto") else "")
                         + (f"\n   {item['contexto']}" if item.get("contexto") else ""))
        fuente = " ".join(str(data.get("fuente") or "").split())[:200]
        title = f"PAGA Suite · dar de alta {len(items)} to-do{'s' if len(items) != 1 else ''}"
        detail = ((f"Fuente: {fuente}\n\n" if fuente else "") + "\n".join(lines)
                  + "\n\nSe dan de alta como to-dos NUEVOS en Level 10 (origen: junta). No edita ninguno existente.")
        return WritePlan("/l10/admin/importar", {"items": items}, title, detail,
                         f"POST /l10/admin/importar con estas {len(items)} filas", "l10/admin/importar")
    if name == "suite_open_week":
        fecha = _clean_date(str(data.get("fecha_junta") or ""))
        if not fecha:
            raise SuiteToolError("fecha_junta is required (YYYY-MM-DD)")
        motivo = " ".join(str(data.get("motivo") or "").split())[:300]
        return WritePlan("/l10/admin/semanas", {"fecha_junta": fecha}, f"PAGA Suite · abrir la semana de la junta "
                         f"del {fecha}", (motivo + "\n\n" if motivo else "") + "Crea la semana de Level 10 y las "
                         "filas de reporte de todos los to-dos vivos.", f"POST /l10/admin/semanas {fecha}",
                         "l10/admin/semanas")
    if name == "suite_close_week":
        try:
            semana = int(data.get("semana_id"))
        except (TypeError, ValueError) as exc:
            raise SuiteToolError("semana_id must be the week's numeric id (suite_read 'contexto')") from exc
        clave = re.sub(r"[^\w\-]", "", str(data.get("clave") or ""))[:20]
        resumen = str(data.get("resumen") or "").strip()[:3000]
        if not resumen:
            raise SuiteToolError("resumen is required: what the week leaves frozen (suite_read 'junta' first)")
        label = clave or f"id {semana}"
        return WritePlan(f"/l10/admin/semanas/{semana}/cerrar", None, f"PAGA Suite · cerrar la semana {label}",
                         resumen + "\n\nAl cerrar, los reportes de la semana se congelan (sólo se corrigen después "
                         "con una versión auditada).", f"POST /l10/admin/semanas/{semana}/cerrar ({label})",
                         f"l10/admin/semanas/{semana}/cerrar")
    raise SuiteToolError(f"unknown Suite tool {name}")


async def execute(plan: WritePlan) -> str:
    from ..argos.checks import CheckNotConfigured
    from ..argos.suite import SuiteError

    try:
        result = await _client().post(plan.path, plan.body)
    except (SuiteError, CheckNotConfigured) as exc:
        raise SuiteToolError(str(exc)) from exc
    return summarize(plan.path, result)


def summarize(path: str, result: Any) -> str:
    r = result if isinstance(result, dict) else {}
    if path.endswith("/importar"):
        text = (f"PAGA Suite: {r.get('creados', 0)} to-do(s) created"
                + (f", {r['omitidos']} skipped" if r.get("omitidos") else ""))
        if r.get("sin_semana_abierta"):
            text += (". There is no open week: the to-dos exist but get their report rows when the next week "
                     "is opened (suite_open_week)")
        warnings = r.get("advertencias") or []
        if warnings:
            text += ".\nSuite warnings:\n- " + "\n- ".join(str(w) for w in warnings[:30])
        return text
    if path.endswith("/cerrar"):
        sem = r.get("semana") or {}
        if r.get("ya_cerrada"):
            return f"PAGA Suite: week {sem.get('clave', '')} was already closed (nothing changed)"
        return f"PAGA Suite: week {sem.get('clave', '')} closed · {r.get('congeladas', 0)} report row(s) frozen"
    sem = r.get("semana") or {}
    text = f"PAGA Suite: week {sem.get('clave', '')} open (id {sem.get('id', '?')}) · " \
           f"{r.get('filas_generadas', 0)} report row(s) created"
    if r.get("advertencias"):
        text += "\nSuite warnings:\n- " + "\n- ".join(str(w) for w in r["advertencias"][:10])
    return text
