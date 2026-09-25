"""Briefs through SCRIBE: the Monday L10 brief (ARGOS) and the CC digest (HERMES) as institutional documents.

Unlike a mission, these briefs are already checked content: the L10 brief's figures are computed by ARGOS and
its prose passes a number guard, and the digest only keeps figures found verbatim in the emails. So SCRIBE does
not rewrite them. The spec is built from them deterministically (no model call) and rendered with the node's brand
kit: the L10 brief as a PDF + a deck to project in the meeting, the digest as a PDF (ATLAS_PUBLISH_FORMATS).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core import paths
from ..core.models import Attachment, Digest, RockStatus
from .brand import load_brand
from .deck import render_deck
from .pdf import DocMeta, render_pdf

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")
SECTION_TITLES_ES = {"rocks": "Rocks", "dashboards": "Tableros de *proyectos*", "l10": "Level *10*",
                     "followups": "*Follow-ups*"}
STATUS_ES = {"ON_TRACK": "On-track", "OFF_TRACK": "Off-track", "AT_RISK": "En riesgo", "FAILED": "Fallido",
             "DONE": "Cumplido", "UNKNOWN": "Sin dato"}
SLIDE_BULLETS = 6
SLIDE_ROWS = 10


def enabled() -> bool:
    """SCRIBE formats briefs unless ATLAS_PUBLISH is off or SCRIBE isn't registered / enabled."""
    if os.getenv("ATLAS_PUBLISH", "on").strip().lower() in ("0", "off", "false", "no"):
        return False
    try:
        from ..core.registry import AgentRegistry

        return any(a.id == "scribe" for a in AgentRegistry.load().all())
    except Exception:  # noqa: BLE001 — a registry problem only means "no documents"
        return False


def formats() -> tuple[str, ...]:
    raw = os.getenv("ATLAS_PUBLISH_FORMATS", "pdf,pptx")
    out = tuple(f for f in (x.strip().lower() for x in raw.split(",")) if f in ("pdf", "pptx"))
    return out or ("pdf", "pptx")


def date_es(d: date | datetime) -> str:
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _clean(line: str) -> str:
    return " ".join(str(line).split())


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".")


def _items(lines: list[str]) -> list[str]:
    """Brief lines → bullet items; indented lines (details) become second-level items (tab prefix)."""
    out = []
    for x in lines:
        text = _clean(x)
        if text:
            out.append(("\t" if str(x)[:1].isspace() and out else "") + text)
    return out


def _chunks(items: list[Any], n: int) -> list[list[Any]]:
    return [items[i:i + n] for i in range(0, len(items), n)] or [[]]


# ---------------------------------------------------------------------------
# L10 brief
# ---------------------------------------------------------------------------


def rocks_table(rocks: list[RockStatus]) -> dict[str, Any]:
    rows = []
    for r in rocks:
        metric = f"{_num(r.current)} / {_num(r.target)}" + (f" {r.metric}" if r.metric else "")
        rows.append([r.title, r.owner, STATUS_ES.get(r.status, r.status), metric,
                     f"{_num(r.required_pace)} / {_num(r.observed_pace)}", r.due.isoformat()])
    return {"columns": ["Rock", "Dueño", "Estado", "Actual / Meta", "Ritmo req. / obs. (sem.)", "Fecha"],
            "rows": rows, "caption": "", "source": "PAGA Suite · módulo Rocks", "truncated": 0}


def brief_spec(week: str, today: date, headline: list[str], sections: dict[str, list[str]],
               rocks: list[RockStatus], counts: dict[str, int], notes: list[str]) -> dict[str, Any]:
    order = ("rocks", "dashboards", "l10", "followups")
    n_rocks = counts.get("rocks", len(rocks))
    on = counts.get("rocks_on_track", 0)
    highlights = []
    if n_rocks:
        highlights.append({"label": "Rocks on-track", "value": f"{on} de {n_rocks}", "note": ""})
    for key, label in (("dashboard_alerts", "Alertas de tableros"), ("overdue_todo", "To-dos vencidos"),
                       ("followups_overdue", "Follow-ups vencidos")):
        if key in counts and len(highlights) < 4:
            highlights.append({"label": label, "value": str(counts[key]), "note": ""})
    doc_sections = []
    for s in order:
        lines = _items(sections.get(s, []))
        blocks: list[dict[str, Any]] = []
        if lines:
            blocks.append({"type": "bullets", "items": lines})
        if s == "rocks" and rocks:
            blocks.append({"type": "table", "table": rocks_table(rocks)})
        if blocks:
            doc_sections.append({"title": SECTION_TITLES_ES[s], "blocks": blocks})
    if notes:
        doc_sections.append({"title": "Notas", "blocks": [{"type": "bullets", "items": [_clean(n) for n in notes]}]})

    num = week.split("-W")[-1].lstrip("0") or week
    slides: list[dict[str, Any]] = []
    if headline:
        slides.append({"type": "statement", "title": "Esta *semana*", "text": _clean(headline[0]), "source": "",
                       "notes": "\n".join(headline)})
    if highlights:
        slides.append({"type": "kpis", "title": "Cifras de la *semana*", "subtitle": "", "kpis": highlights,
                       "source": "", "notes": ""})
    for s in order:
        lines = _items(sections.get(s, []))
        chunks = _chunks(lines, SLIDE_BULLETS) if lines else []
        for i, chunk in enumerate(chunks):
            slides.append({"type": "bullets", "title": SECTION_TITLES_ES[s] + (" (cont.)" if i else ""),
                           "subtitle": "", "items": chunk, "source": "", "notes": ""})
        if s == "rocks" and rocks:
            table = rocks_table(rocks)
            for i, rows in enumerate(_chunks(table["rows"], SLIDE_ROWS)):
                slides.append({"type": "table", "title": "Estado de *Rocks*" + (" (cont.)" if i else ""),
                               "subtitle": "", "table": {**table, "rows": rows}, "source": table["source"],
                               "notes": ""})
    return {
        "doc_kind": "Brief semanal L10",
        "title": f"Brief L10 · semana *{num}*",
        "subtitle": f"Semana ISO {week} · preparado por ARGOS",
        "summary": [_clean(h) for h in headline] or ["Sin novedades que reportar esta semana."],
        "highlights": highlights,
        "sections": doc_sections or [{"title": "Sin datos", "blocks": [
            {"type": "paragraph", "text": "No hubo datos para esta semana."}]}],
        "slides": slides,
        "sources": ["PAGA Suite (L10 y Rocks)", "Reportes semanales de proyectos recibidos por correo",
                    "Tablero de follow-ups de ATLAS"],
    }


# ---------------------------------------------------------------------------
# CC digest
# ---------------------------------------------------------------------------

_IMPORTANCE_ES = {"HIGH": "Alta", "MEDIUM": "Media", "LOW": "Baja"}


def digest_spec(d: Digest) -> dict[str, Any]:
    start = d.window_start.date() if d.window_start else None
    end = d.window_end.date() if d.window_end else None
    window = (f"Del {date_es(start)} al {date_es(end)}" if start and end and start != end
              else date_es(end or start) if (end or start) else "")
    sections = []
    for t in d.threads:
        blocks: list[dict[str, Any]] = []
        meta = " · ".join(x for x in (t.project or "", f"Importancia {_IMPORTANCE_ES.get(str(t.importance), t.importance)}",
                                      ", ".join(t.participants[:5])) if x)
        if meta:
            blocks.append({"type": "paragraph", "text": meta})
        if t.asks_me:
            blocks.append({"type": "callout", "title": "Te piden", "text": _clean(t.asks_me)})
        if t.summary:
            blocks.append({"type": "bullets", "items": [_clean(x) for x in t.summary]})
        if t.decisions:
            blocks.append({"type": "callout", "title": "Decisiones", "text": " · ".join(_clean(x) for x in t.decisions)})
        if t.figures:
            blocks.append({"type": "bullets", "items": [f"**Cifra:** {_clean(x)}" for x in t.figures]})
        sections.append({"title": _clean(t.subject)[:110] or "(sin asunto)", "blocks": blocks or [
            {"type": "paragraph", "text": "Sin resumen."}]})
    asks = sum(1 for t in d.threads if t.asks_me)
    highlights = [{"label": "Conversaciones", "value": str(len(d.threads)), "note": ""},
                  {"label": "Te piden algo", "value": str(asks), "note": "creados como follow-ups"}]
    if d.skipped:
        highlights.append({"label": "Excluidos", "value": str(d.skipped), "note": "automáticos o por reglas"})
    return {
        "doc_kind": "Resumen de correos en copia",
        "title": "Correos en *copia*",
        "subtitle": window,
        "summary": [_clean(h) for h in d.headline] or ["Sin conversaciones relevantes en el periodo."],
        "highlights": highlights,
        "sections": sections or [{"title": "Sin conversaciones", "blocks": [
            {"type": "paragraph", "text": "No hubo correos en copia que resumir en el periodo."}]}],
        "slides": [],
        "sources": ["Correo de Outlook (los cuerpos de los correos no se almacenan)"],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(spec: dict[str, Any], node: str, folder: Path, stem: str, fmts: tuple[str, ...],
           when: date | datetime, notes: dict[str, list[str]] | None = None) -> list[Path]:
    """Render `spec` into folder/<stem>.<fmt> for each format (never overwriting). Returns the files."""
    brand = load_brand(node)
    folder.mkdir(parents=True, exist_ok=True)
    date_text = date_es(when)
    out: list[Path] = []
    for fmt in fmts:
        if fmt == "pptx" and not spec.get("slides"):
            continue
        target, n = folder / f"{stem}.{fmt}", 2
        while target.exists():
            target, n = folder / f"{stem} ({n}).{fmt}", n + 1
        if fmt == "pdf":
            render_pdf(spec, brand, DocMeta(date_text=date_text, notes=notes or {}, author_line=brand.prepared_by),
                       target)
        else:
            closing = [x for x in (brand.company, brand.prepared_by, date_text, brand.footer) if x]
            render_deck(spec, brand, date_text, closing, target)
        out.append(target)
    return out


def prefix(node: str) -> str:
    company = load_brand(node).company
    return paths.safe_name((company.split()[0] if company else "ATLAS").upper())


MEDIA = {".pdf": "application/pdf",
         ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


def document_path(docs: list[Attachment], name: str, folder: Path) -> Path | None:
    """The file behind one of `docs` (by name), only inside `folder` (for the download routes)."""
    if not any(d.name == name for d in docs):
        return None
    target = folder / paths.safe_name(name)
    return target if target.is_file() and paths.is_within(target, folder) else None


def attachment(path: Path, url: str) -> Attachment:
    return Attachment(name=path.name, kind="file", size_bytes=path.stat().st_size, download_url=url)
