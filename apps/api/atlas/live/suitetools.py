"""The agents' PAGA Suite tools: Level 10 to-dos, issues, Rocks (docs/SUITE_TOOLS.md).

    suite_read(what, ...)          read the L10 module: to-dos, issues, the meeting view, the week, Rocks
    suite_add_todos(todos, ...)    add NEW to-dos (never edits an existing one)       } each one asks the human
    suite_report_todos(...)        capture the open week's reports from the meeting   } in ATLAS first and runs
    suite_open_week(fecha_junta)   open the L10 week of a meeting date                } only if approved
    suite_close_week(semana_id)    close a week: freezes its reports                  }

Reads use the same connection as ARGOS (ATLAS_SUITE_URL + ATLAS_SUITE_TOKEN / ATLAS_SUITE_SCOPE). Writes also need
ATLAS_SUITE_WRITE=1 here and ATLAS_API_KEY_WRITE=1 in the Suite (paga-app api/main.py `_atlas_puede`): the Suite's
key then accepts exactly POST /l10/admin/importar (new to-dos only), PUT /l10/reportes/{id} (the open week's
reports, always with a comment, never assigning help to a person), POST /l10/admin/semanas and
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
MAX_REPORTS = 60
ESTATUS = ("no_iniciado", "en_proceso", "detenido_tercero", "detenido_area", "cumplido", "no_aplica")
APOYO_AREAS = ("direccion_general", "legal", "finanzas", "construccion", "proyectos", "ventas", "cobranza",
               "tecnologia", "externo")
COMMENT_MAX = 1500
TODO_FIELDS = ("titulo", "responsable", "fecha_compromiso", "proyecto", "torre", "contexto", "decide",
               "involucrados")


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
        "Suite's catalog; '' = corporativo), torre (only for SONOMA: PIETRA/FIORI/ACQUA and BALCONES: B600/B200, when the meeting names "
        "the tower; suite_read 'contexto' lists them under torres), contexto (one line: where it came from, e.g. the minute of the "
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

SUITE_REPORT_TODOS_TOOL: dict[str, Any] = {
    "name": "suite_report_todos",
    "description": (
        "Capture, in the OPEN Level 10 week, the status of existing to-dos as it was said in the weekly meeting "
        "(what got done in the meeting itself, progress, blockers, what was taken to IDS). Do it BEFORE "
        "suite_close_week: a closed week is frozen. The meeting of a Monday reviews the week that is still open "
        "(opened at the previous meeting); suite_read 'contexto' / 'junta' give its id and each to-do's current "
        "report. One row per to-do that the meeting actually talked about, by its codigo (PC-012): estatus "
        "(no_iniciado, en_proceso, detenido_tercero, detenido_area, cumplido, no_aplica), avance_pct 0-100 only "
        "if a figure or 'done' was said, requiere_apoyo + apoyo_area when someone asked for help from an area, "
        "and comentario: what was said, with the minute of the transcript (e.g. '12:40 Melanie: ya se mandó la "
        "lista'). Never report a to-do the meeting didn't mention and never guess a status. It's appended to "
        "the existing comment (the person's own report stays). Asks the human for approval itself, showing the "
        "before → after of each row; don't call request_approval for it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "semana_id": {"type": "integer", "description": "the open week's id"},
            "fuente": {"type": "string", "description": "e.g. 'Junta semanal 2026-10-05 (transcripción)'"},
            "reportes": {
                "type": "array", "maxItems": MAX_REPORTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "codigo": {"type": "string"},
                        "estatus": {"type": "string", "enum": list(ESTATUS)},
                        "avance_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                        "requiere_apoyo": {"type": "boolean"},
                        "apoyo_area": {"type": "string", "enum": list(APOYO_AREAS)},
                        "comentario": {"type": "string"},
                    },
                    "required": ["codigo", "comentario"],
                },
            },
        },
        "required": ["semana_id", "fuente", "reportes"],
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
WRITE_TOOLS = [SUITE_ADD_TODOS_TOOL, SUITE_REPORT_TODOS_TOOL, SUITE_OPEN_WEEK_TOOL, SUITE_CLOSE_WEEK_TOOL]
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
        text += (" suite_add_todos adds new to-dos, suite_report_todos captures the open week's status of the "
                 "to-dos the meeting discussed (before closing it), suite_open_week / suite_close_week open and "
                 "close the week: each asks the human for approval inside the tool (no separate "
                 "request_approval). Weekly order: report the open week from the meeting → close it → open the "
                 "new week → add the new to-dos.")
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
    method: str = "POST"
    batch: list[tuple[str, dict[str, Any], str]] | None = None  # (path, body, label) — one PUT per row


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


async def plan(name: str, data: dict[str, Any]) -> WritePlan:
    """plan_write, plus the writes that need to read the Suite first to show the human before → after."""
    if name == "suite_report_todos":
        return await _plan_report(data)
    return plan_write(name, data)


def _fmt_report(row: dict[str, Any]) -> str:
    est = row.get("estatus") or "sin reporte"
    pct = row.get("avance_pct")
    return est + (f" {pct}%" if pct is not None else "") + (" · pide apoyo" if row.get("requiere_apoyo") else "")


async def _plan_report(data: dict[str, Any]) -> WritePlan:
    from ..argos.checks import CheckNotConfigured
    from ..argos.suite import SuiteError

    if not write_enabled():
        raise SuiteToolError("Writing to PAGA Suite is off (ATLAS_SUITE_WRITE=1 in ATLAS's .env turns it on)")
    try:
        semana_id = int(data.get("semana_id"))
    except (TypeError, ValueError) as exc:
        raise SuiteToolError("semana_id must be the open week's numeric id (suite_read 'contexto')") from exc
    rows = data.get("reportes")
    if not isinstance(rows, list) or not rows:
        raise SuiteToolError("reportes must be a non-empty list")
    if len(rows) > MAX_REPORTS:
        raise SuiteToolError(f"at most {MAX_REPORTS} reports per call")
    fuente = " ".join(str(data.get("fuente") or "").split())[:120]
    if not fuente:
        raise SuiteToolError("fuente is required (e.g. 'Junta semanal 2026-10-05')")
    try:
        junta = await _client().get(f"/l10/junta?semana_id={semana_id}")
    except (SuiteError, CheckNotConfigured) as exc:
        raise SuiteToolError(str(exc)) from exc
    semana = (junta or {}).get("semana") or {}
    if not semana:
        raise SuiteToolError(f"week {semana_id} not found")
    clave = semana.get("clave") or f"id {semana_id}"
    if semana.get("estado") != "abierta":
        raise SuiteToolError(f"week {clave} is {semana.get('estado')}: its reports are frozen. Status can only be "
                             "captured in an OPEN week, before suite_close_week")
    by_code = {str(f.get("codigo") or "").upper(): f for g in junta.get("grupos") or [] for f in g.get("filas") or []}
    batch, lines, seen = [], [], set()
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise SuiteToolError(f"report {n} is not an object")
        code = str(row.get("codigo") or "").strip().upper()
        fila = by_code.get(code)
        if fila is None:
            raise SuiteToolError(f"{code or f'report {n}'} is not in week {clave} (suite_read 'junta' for its codes)")
        if code in seen:
            raise SuiteToolError(f"{code} appears twice: one row per to-do")
        seen.add(code)
        if fila.get("congelado"):
            raise SuiteToolError(f"{code}'s report is frozen")
        said = " ".join(str(row.get("comentario") or "").split())
        if len(said) < 5:
            raise SuiteToolError(f"{code}: comentario must say what was said in the meeting (with the minute)")
        body: dict[str, Any] = {}
        est = str(row.get("estatus") or "").strip()
        if est:
            if est not in ESTATUS:
                raise SuiteToolError(f"{code}: estatus must be one of {', '.join(ESTATUS)}")
            body["estatus"] = est
        if row.get("avance_pct") is not None:
            try:
                pct = int(row["avance_pct"])
            except (TypeError, ValueError) as exc:
                raise SuiteToolError(f"{code}: avance_pct must be 0-100") from exc
            if not 0 <= pct <= 100:
                raise SuiteToolError(f"{code}: avance_pct must be 0-100")
            body["avance_pct"] = pct
        if row.get("requiere_apoyo") is not None:
            body["requiere_apoyo"] = bool(row["requiere_apoyo"])
        area = str(row.get("apoyo_area") or "").strip()
        if area:
            if area not in APOYO_AREAS:
                raise SuiteToolError(f"{code}: apoyo_area must be one of {', '.join(APOYO_AREAS)}")
            body["apoyo_area"] = area
        old = str(fila.get("comentario") or "").strip()
        note = f"[{fuente}] {said}"
        combined = f"{old}\n{note}" if old else note
        body["comentario"] = combined[-COMMENT_MAX:]  # the meeting's note is never the part cut
        after = {**fila, **{k: v for k, v in body.items() if k != "comentario"}}
        if after.get("estatus") == "cumplido" and "avance_pct" not in body:
            after["avance_pct"] = 100
        title = " ".join(str(fila.get("titulo") or "").split())[:80]
        who = fila.get("responsable_nombre") or fila.get("responsable_email") or "sin dueño"
        lines.append(f"{code} · {title} ({who})\n   {_fmt_report(fila)} → {_fmt_report(after)}\n   «{said[:300]}»")
        batch.append((f"/l10/reportes/{int(fila['todo_semana_id'])}", body, code))
    detail = (f"Semana {clave} (abierta) · fuente: {fuente}\n\n" + "\n".join(lines)
              + "\n\nSe captura como reporte de la junta (queda registrado como capturado por ATLAS, no como "
                "autoreporte). El comentario se agrega al que ya tenía cada fila.")
    n = len(batch)
    return WritePlan(f"/l10/reportes ({n})", None, f"PAGA Suite · capturar {n} reporte{'s' if n != 1 else ''} de "
                     f"la junta en {clave}", detail, f"PUT /l10/reportes/{{id}} × {n} en {clave}",
                     f"l10/reportes × {n} ({clave})", method="PUT", batch=batch)


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
                         + (f" · torre: {item['torre']}" if item.get("torre") else "")
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

    if plan.batch is not None:
        client = _client()
        done, failed = [], []
        for path, body, label in plan.batch:
            try:
                await client.request(plan.method, path, body)
                done.append(label)
            except (SuiteError, CheckNotConfigured) as exc:
                failed.append(f"{label}: {exc}")
        text = f"PAGA Suite: {len(done)} report(s) captured" + (f" ({', '.join(done)})" if done else "")
        if failed:
            text += f"; {len(failed)} failed:\n- " + "\n- ".join(failed)
            if not done:
                raise SuiteToolError(text)
        return text
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
