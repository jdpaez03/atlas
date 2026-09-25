"""The document spec SCRIBE writes (tool `submit_documents`) and its validation.

SCRIBE decides WHAT goes in the documents (structure, wording, which tables and charts); the renderers
(pdf.py, deck.py) decide HOW it looks. One spec feeds both the PDF report and the committee deck.
"""

from __future__ import annotations

import re
from typing import Any

MAX_SECTIONS = 12
MAX_BLOCKS = 14
MAX_SLIDES = 22
MAX_TABLE_ROWS = 60
MAX_DECK_TABLE_ROWS = 40  # split across slides by style.table_rows_per_slide
MAX_COLS = 9
MAX_KPIS = 4
MAX_SERIES = 4
MAX_CATEGORIES = 24

_TEXT = {"type": "string"}
_TEXTS = {"type": "array", "items": {"type": "string"}}
_KPI = {
    "type": "object",
    "properties": {"label": _TEXT, "value": {"type": "string", "description": "as it should read: '72,300 MXN/m²'"},
                   "note": _TEXT},
    "required": ["label", "value"],
}
_TABLE = {
    "type": "object",
    "properties": {
        "columns": _TEXTS,
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number", "null"]}}},
        "caption": _TEXT,
        "source": {"type": "string", "description": "where the numbers come from (agent report, file, site)"},
    },
    "required": ["columns", "rows"],
}
_CHART = {
    "type": "object",
    "properties": {
        "chart_type": {"type": "string", "enum": ["bar", "line"]},
        "title": _TEXT,
        "categories": _TEXTS,
        "series": {"type": "array", "items": {"type": "object", "properties": {
            "name": _TEXT, "values": {"type": "array", "items": {"type": ["number", "null"]}}},
            "required": ["name", "values"]}},
        "unit": _TEXT,
        "source": _TEXT,
    },
    "required": ["chart_type", "categories", "series"],
}
_BLOCK = {
    "type": "object",
    "description": "one of: paragraph {text} · bullets {items} · table {table} · kpis {kpis} · "
                   "callout {title, text} · chart {chart}",
    "properties": {
        "type": {"type": "string", "enum": ["paragraph", "bullets", "table", "kpis", "callout", "chart"]},
        "text": _TEXT, "items": _TEXTS, "title": _TEXT,
        "table": _TABLE, "kpis": {"type": "array", "items": _KPI}, "chart": _CHART,
    },
    "required": ["type"],
}
_SLIDE = {
    "type": "object",
    "description": (
        "section {title} · bullets {title, subtitle?, items} · table {title, table} · kpis {title, kpis} · "
        "chart {title, chart} · statement {text} (one big sentence) · quote {text, attribution}. "
        "Titles may mark ONE accent word in *asterisks* (rendered in italics)."
    ),
    "properties": {
        "type": {"type": "string", "enum": ["section", "bullets", "table", "kpis", "chart", "statement", "quote"]},
        "title": _TEXT, "subtitle": _TEXT, "items": _TEXTS, "text": _TEXT, "attribution": _TEXT,
        "table": _TABLE, "kpis": {"type": "array", "items": _KPI}, "chart": _CHART,
        "source": _TEXT, "notes": {"type": "string", "description": "speaker notes"},
    },
    "required": ["type"],
}

SUBMIT_DOCUMENTS_TOOL: dict[str, Any] = {
    "name": "submit_documents",
    "description": "Submit the content of the institutional documents: the PDF report (summary + sections) and the "
                   "committee deck (slides). The system lays them out in the company's style.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_kind": {"type": "string", "description": "e.g. 'Estudio de mercado', 'Reporte ejecutivo'"},
            "title": {"type": "string", "description": "document title; may mark one accent word in *asterisks*"},
            "subtitle": _TEXT,
            "summary": {"type": "array", "items": {"type": "string"},
                        "description": "executive summary: 1-4 short paragraphs, answer first"},
            "highlights": {"type": "array", "items": _KPI, "description": "0-4 headline figures"},
            "sections": {"type": "array", "items": {"type": "object", "properties": {
                "title": _TEXT, "blocks": {"type": "array", "items": _BLOCK}}, "required": ["title", "blocks"]}},
            "slides": {"type": "array", "items": _SLIDE,
                       "description": "the deck body (cover and closing are added by the system)"},
            "sources": {"type": "array", "items": {"type": "string"}},
            "style": {
                "type": "object",
                "description": "layout choices for THIS document (set them from the human's lessons; omit = defaults)",
                "properties": {
                    "deck_density": {"type": "string", "enum": ["airy", "standard", "compact"],
                                     "description": "text size on slides: airy = bigger, compact = smaller"},
                    "table_rows_per_slide": {"type": "integer", "minimum": 4, "maximum": 14,
                                             "description": "long tables continue on the next slide (default 8)"},
                    "agenda": {"type": "boolean", "description": "agenda slide after the cover (default false)"},
                    "toc": {"type": "boolean", "description": "table of contents page in the PDF (default false)"},
                    "chart_data_labels": {"type": "boolean", "description": "values printed on bars/points"},
                    "section_numbers": {"type": "boolean", "description": "01, 02… before PDF sections (default true)"},
                },
            },
        },
        "required": ["title", "summary", "sections", "slides"],
    },
}


def _s(v: Any, n: int = 4000) -> str:
    return " ".join(str(v if v is not None else "").split())[:n]


def _list(v: Any, n: int, width: int = 600) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [_s(x, width) for x in (v if isinstance(v, list) else []) if _s(x, width)][:n]


def _kpis(v: Any) -> list[dict[str, str]]:
    out = []
    for k in v if isinstance(v, list) else []:
        if isinstance(k, dict) and _s(k.get("value")):
            out.append({"label": _s(k.get("label"), 60), "value": _s(k.get("value"), 30), "note": _s(k.get("note"), 90)})
    return out[:MAX_KPIS]


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return v
    return _s(v, 120)


def _table(v: Any, errors: list[str], where: str, max_rows: int = MAX_TABLE_ROWS) -> dict[str, Any] | None:
    if not isinstance(v, dict):
        errors.append(f"{where}: table must be an object with columns and rows")
        return None
    cols = _list(v.get("columns"), MAX_COLS, 60)
    rows = [r for r in (v.get("rows") or []) if isinstance(r, list)]
    if not cols:
        errors.append(f"{where}: table has no columns")
        return None
    if not rows:
        errors.append(f"{where}: table has no rows")
        return None
    norm = [[_cell(c) for c in (r + [""] * len(cols))[: len(cols)]] for r in rows[:max_rows]]
    return {"columns": cols, "rows": norm, "caption": _s(v.get("caption"), 200), "source": _s(v.get("source"), 240),
            "truncated": max(0, len(rows) - max_rows)}


def _chart(v: Any, errors: list[str], where: str) -> dict[str, Any] | None:
    if not isinstance(v, dict):
        errors.append(f"{where}: chart must be an object")
        return None
    cats = _list(v.get("categories"), MAX_CATEGORIES, 40)
    series = []
    for sr in (v.get("series") or [])[:MAX_SERIES]:
        if not isinstance(sr, dict):
            continue
        vals = sr.get("values") if isinstance(sr.get("values"), list) else []
        clean = []
        for x in vals[: len(cats)]:
            try:
                clean.append(None if x is None or x == "" else float(x))
            except (TypeError, ValueError):
                clean.append(None)
        if len(clean) != len(cats):
            errors.append(f"{where}: series '{sr.get('name')}' has {len(vals)} values for {len(cats)} categories")
            continue
        series.append({"name": _s(sr.get("name"), 40) or "Serie", "values": clean})
    if not cats or not series:
        errors.append(f"{where}: chart needs categories and at least one series of numbers")
        return None
    kind = str(v.get("chart_type") or "bar").lower()
    return {"chart_type": kind if kind in ("bar", "line") else "bar", "title": _s(v.get("title"), 120),
            "categories": cats, "series": series, "unit": _s(v.get("unit"), 30), "source": _s(v.get("source"), 240)}


def _block(b: Any, errors: list[str], where: str) -> dict[str, Any] | None:
    if not isinstance(b, dict):
        return None
    t = str(b.get("type") or "").lower()
    if t == "paragraph" and _s(b.get("text")):
        return {"type": t, "text": _s(b.get("text"), 3000)}
    if t == "bullets" and _list(b.get("items"), 12):
        return {"type": t, "items": _list(b.get("items"), 12)}
    if t == "callout" and _s(b.get("text")):
        return {"type": t, "title": _s(b.get("title"), 100), "text": _s(b.get("text"), 1200)}
    if t == "kpis" and _kpis(b.get("kpis")):
        return {"type": t, "kpis": _kpis(b.get("kpis"))}
    if t == "table":
        tb = _table(b.get("table"), errors, where)
        return {"type": t, "table": tb} if tb else None
    if t == "chart":
        ch = _chart(b.get("chart"), errors, where)
        return {"type": t, "chart": ch} if ch else None
    errors.append(f"{where}: block type '{t}' is missing its content")
    return None


def _slide(sl: Any, errors: list[str], where: str) -> dict[str, Any] | None:
    if not isinstance(sl, dict):
        return None
    t = str(sl.get("type") or "").lower()
    out: dict[str, Any] = {"type": t, "title": _s(sl.get("title"), 120), "subtitle": _s(sl.get("subtitle"), 200),
                           "source": _s(sl.get("source"), 240), "notes": _s(sl.get("notes"), 3000)}
    if t == "section" and out["title"]:
        return out
    if t == "bullets" and _list(sl.get("items"), 7, 240):
        return {**out, "items": _list(sl.get("items"), 7, 240)}
    if t == "table":
        tb = _table(sl.get("table"), errors, where, max_rows=MAX_DECK_TABLE_ROWS)
        return {**out, "table": tb} if tb else None
    if t == "kpis" and _kpis(sl.get("kpis")):
        return {**out, "kpis": _kpis(sl.get("kpis"))}
    if t == "chart":
        ch = _chart(sl.get("chart"), errors, where)
        return {**out, "chart": ch} if ch else None
    if t in ("statement", "quote") and _s(sl.get("text")):
        return {**out, "text": _s(sl.get("text"), 400), "attribution": _s(sl.get("attribution"), 120)}
    errors.append(f"{where}: slide type '{t}' is missing its content")
    return None


DEFAULT_STYLE: dict[str, Any] = {"deck_density": "standard", "table_rows_per_slide": 8, "agenda": False,
                                 "toc": False, "chart_data_labels": False, "section_numbers": True}


def normalize_style(v: Any) -> dict[str, Any]:
    out = dict(DEFAULT_STYLE)
    if not isinstance(v, dict):
        return out
    if v.get("deck_density") in ("airy", "standard", "compact"):
        out["deck_density"] = v["deck_density"]
    try:
        out["table_rows_per_slide"] = min(14, max(4, int(v.get("table_rows_per_slide") or 8)))
    except (TypeError, ValueError):
        pass
    for k in ("agenda", "toc", "chart_data_labels", "section_numbers"):
        if isinstance(v.get(k), bool):
            out[k] = v[k]
    return out


def normalize(data: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """(clean spec, errors). Errors go back to SCRIBE; a spec with errors is still renderable."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return {}, ["call submit_documents with the document content"]
    spec: dict[str, Any] = {
        "doc_kind": _s(data.get("doc_kind"), 60),
        "title": _s(data.get("title"), 160),
        "subtitle": _s(data.get("subtitle"), 240),
        "summary": _list(data.get("summary"), 4, 1600),
        "highlights": _kpis(data.get("highlights")),
        "sources": _list(data.get("sources"), 30, 300),
        "sections": [],
        "slides": [],
        "style": normalize_style(data.get("style")),
    }
    if not spec["title"]:
        errors.append("title is required")
    if not spec["summary"]:
        errors.append("summary is required (1-4 paragraphs, answer first)")
    for i, sec in enumerate((data.get("sections") or [])[:MAX_SECTIONS], 1):
        if not isinstance(sec, dict) or not _s(sec.get("title")):
            errors.append(f"section {i} needs a title")
            continue
        blocks = [b for j, raw in enumerate((sec.get("blocks") or [])[:MAX_BLOCKS], 1)
                  if (b := _block(raw, errors, f"section {i} block {j}"))]
        if blocks:
            spec["sections"].append({"title": _s(sec.get("title"), 120), "blocks": blocks})
    if not spec["sections"]:
        errors.append("at least one section with content is required")
    for i, raw in enumerate((data.get("slides") or [])[:MAX_SLIDES], 1):
        sl = _slide(raw, errors, f"slide {i}")
        if sl:
            spec["slides"].append(sl)
    if not spec["slides"]:
        errors.append("the deck needs slides (8-14 for a committee is typical)")
    return spec, errors


def spec_text(spec: dict[str, Any]) -> str:
    """Every word and figure in the documents (for the traceability check)."""
    parts: list[str] = [spec.get("title", ""), spec.get("subtitle", ""), *spec.get("summary", [])]
    parts += [f"{k['label']} {k['value']} {k['note']}" for k in spec.get("highlights", [])]

    def table(t: dict[str, Any]) -> None:
        parts.extend(" ".join(str(c) for c in r) for r in t["rows"])

    def chart(c: dict[str, Any]) -> None:
        parts.extend(" ".join("" if v is None else _num(v) for v in s["values"]) for s in c["series"])

    for sec in spec.get("sections", []):
        for b in sec["blocks"]:
            parts += [b.get("text", ""), *b.get("items", [])]
            parts += [f"{k['value']}" for k in b.get("kpis", [])]
            if b.get("table"):
                table(b["table"])
            if b.get("chart"):
                chart(b["chart"])
    for sl in spec.get("slides", []):
        parts += [sl.get("title", ""), sl.get("subtitle", ""), sl.get("text", ""), *sl.get("items", [])]
        parts += [f"{k['value']}" for k in sl.get("kpis", [])]
        if sl.get("table"):
            table(sl["table"])
        if sl.get("chart"):
            chart(sl["chart"])
    return "\n".join(p for p in parts if p)


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


_ACCENT = re.compile(r"\*([^*]+)\*")


def accent_runs(text: str) -> list[tuple[str, bool]]:
    """'Nuestra *huella*' -> [('Nuestra ', False), ('huella', True)]."""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _ACCENT.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [("", False)]


def plain(text: str) -> str:
    return _ACCENT.sub(r"\1", text or "")
