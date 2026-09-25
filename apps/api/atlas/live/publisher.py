"""SCRIBE: the mission's final report → institutional documents (docs/PUBLISHING.md).

After consolidation, SCRIBE writes one document spec (submit_documents) from the executive report, the agent
reports and excerpts of the data files the agents produced. The system then renders it with the node's brand kit
as a PDF report and a committee deck (ATLAS_PUBLISH_FORMATS), saves them to the mission outputs, records them as
evidence and attaches them to the report (`MissionReport.documents`).

Figures in the documents are checked like the executive report: any number that traces to no agent report,
evidence, data file or human note is listed in the PDF's verification notes and in the mission log.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ..core import paths
from ..core.models import AgentStatus, Attachment, MissionReport
from ..publish.brand import Brand, load_brand
from ..publish.deck import render_deck
from ..publish.pdf import DocMeta, render_pdf
from ..publish.spec import SUBMIT_DOCUMENTS_TOOL, normalize, plain, spec_text
from . import auditor as audit_mod
from .evidence import record
from .files import FileAccessError, _size, download_url, extract_text
from .lessons import with_lessons
from .llm import LLMError, current_agent
from .prompts import render_mission_report

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")
DATA_EXTS = {".csv", ".xlsx", ".xlsm", ".md", ".txt", ".json", ".docx"}
MAX_EXCERPT = 5000
MAX_EXCERPTS = 24000


def date_es(when: Any = None) -> str:
    from datetime import datetime

    from ..inbox.state import user_tz

    d = when or datetime.now(user_tz())
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def data_excerpts(node: str, mission_id: str, report: MissionReport) -> list[str]:
    """Text of the data files the agents delivered (tables SCRIBE can lay out), capped."""
    out: list[str] = []
    total = 0
    folder = paths.outputs_dir(node, mission_id)
    for att in report.deliverables:
        p = folder / att.name
        if p.suffix.lower() not in DATA_EXTS or not p.is_file():
            continue
        try:
            text, _ = extract_text(p)
        except (FileAccessError, OSError, ValueError):
            continue
        chunk = text[:MAX_EXCERPT] + ("\n… (truncated)" if len(text) > MAX_EXCERPT else "")
        if total + len(chunk) > MAX_EXCERPTS:
            break
        total += len(chunk)
        out.append(f"### {att.name}\n{chunk}")
    return out


def publish_message(objective: str, brand: Brand, report: MissionReport, reports: list[str], excerpts: list[str],
                    notes: list[str], date_text: str) -> str:
    parts = [
        "STAGE: PUBLISHING",
        f"Mission objective: {objective}",
        f"Organization: {brand.company or '(not set)'} · Date: {date_text}",
        "## Final mission report (ATLAS)",
        render_mission_report(report),
    ]
    for title, items in (("Next actions", report.next_actions), ("Conflicts", report.conflicts),
                         ("Assumptions", report.assumptions), ("Needs human attention", report.needs_human_attention),
                         ("References", report.references)):
        if items:
            parts.append(f"{title}:\n" + "\n".join(f"- {x}" for x in items))
    if report.audit_summary:
        parts.append(f"Audit: {report.audit_summary}")
    parts.append("## Agent reports")
    parts += reports or ["(none)"]
    if excerpts:
        parts.append("## Data files the agents delivered (excerpts)")
        parts += excerpts
    if notes:
        parts.append("## Human notes in the mission thread")
        parts += [f"- {n}" for n in notes]
    parts.append("Write the institutional documents now and call submit_documents.")
    return "\n\n".join(parts)


def _slug(text: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", plain(text), flags=re.UNICODE)
    s = re.sub(r"\s+", "_", s.strip())
    import unicodedata

    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s[:n].strip("_") or "Documento"


def _unique(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    p, n = folder / name, 2
    while p.exists():
        p, n = folder / f"{stem} ({n}){ext}", n + 1
    return p


def verification_notes(report: MissionReport, untraced: list[str]) -> dict[str, list[str]]:
    verify: list[str] = []
    if report.audit_summary:
        verify.append(report.audit_summary)
    verify += [f"Cifra sin respaldo rastreable en los reportes: {u}" for u in untraced]
    return {
        "Supuestos": report.assumptions,
        "Conflictos entre fuentes": report.conflicts,
        "Verificación": verify,
        "Requiere atención": report.needs_human_attention,
    }


async def publish(live: Any, report: MissionReport) -> list[Attachment]:
    """Run SCRIBE and render. Raises LLMError when the step fails; returns the documents' attachments."""
    scope = live.scope
    pub = scope.publisher
    store = scope.store
    cfg = scope.config
    brand = load_brand(scope.node)
    date_text = date_es()
    await scope.set_agent(pub.id, AgentStatus.WORKING, activity="Formatting the institutional documents")
    excerpts = await asyncio.to_thread(data_excerpts, scope.node, scope.mission_id, report)
    prompt = publish_message(scope.objective, brand, report, live._reports_text(), excerpts, live._notes(), date_text)

    async def validate(data: dict[str, Any] | None) -> list[str]:
        return normalize(data)[1]

    token = current_agent.set(pub.id)
    try:
        data = await live.executor.structured(
            scope, model=pub.model, system=with_lessons([pub.role_prompt], store, pub.id, scope.node), prompt=prompt, tool=SUBMIT_DOCUMENTS_TOOL,
            max_tokens=cfg.publish_max_tokens, validate=validate, attempts=2,
        )
    finally:
        current_agent.reset(token)
    spec, errors = normalize(data)
    if not spec.get("title") or not spec.get("sections"):
        raise LLMError("SCRIBE did not submit usable documents" + (f": {'; '.join(errors[:3])}" if errors else ""))
    if not spec["slides"] and "pptx" in cfg.publish_formats:
        spec["slides"] = [{"type": "bullets", "title": sec["title"], "subtitle": "", "source": "", "notes": "",
                           "items": [b.get("text", "")[:240] for b in sec["blocks"] if b.get("text")][:5] or ["—"]}
                          for sec in spec["sections"]]

    corpus = audit_mod.report_corpus(live) + [render_mission_report(report), *excerpts]
    probe = report.model_copy(update={"executive_summary": spec_text(spec), "key_findings": []})
    untraced = audit_mod.untraced_figures(probe, corpus, limit=12)
    if untraced:
        await store.log(f"SCRIBE · {len(untraced)} figure(s) in the documents trace to no agent report: "
                        + "; ".join(u.split(" — ")[0] for u in untraced[:5]),
                        mission_id=scope.mission_id, agent_id=pub.id)

    folder = paths.outputs_dir(scope.node, scope.mission_id)
    prefix = _slug((brand.company or "ATLAS").split()[0], 20).upper()
    base = f"{prefix}_{_slug(spec['title'])}" + (f"_v{report.version}" if report.version > 1 else "")
    meta = DocMeta(date_text=date_text, notes=verification_notes(report, untraced), author_line=brand.prepared_by)
    closing = [x for x in (brand.company, brand.prepared_by, date_text, brand.footer) if x]
    docs: list[Attachment] = []
    for fmt in cfg.publish_formats:
        target = _unique(folder, f"{base}_{'Reporte' if fmt == 'pdf' else 'Presentacion'}.{fmt}")
        if fmt == "pdf":
            await asyncio.to_thread(render_pdf, spec, brand, meta, target)
            label = "PDF institucional"
        else:
            await asyncio.to_thread(render_deck, spec, brand, date_text, closing, target)
            label = f"presentación de {len(spec['slides']) + 2} diapositivas"
        size = target.stat().st_size
        att = Attachment(name=target.name, kind="file", size_bytes=size,
                         download_url=download_url(scope.mission_id, target.name))
        docs.append(att)
        await record(store, mission_id=scope.mission_id, task_id=None, agent_id=pub.id, kind="file_written",
                     ref=str(target), detail=f"{_size(size)} · {label}")
    await scope.set_agent(pub.id, AgentStatus.COMPLETED,
                          activity=f"Published {len(docs)} document(s)" + (
                              f" · {len(untraced)} figure(s) to check" if untraced else ""))
    return docs
