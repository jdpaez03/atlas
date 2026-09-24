"""Evidence: what agents actually did, recorded by the system (docs/PHASE3.md § A).

    record(...)               one Evidence item through store.record_evidence (+ a readable feed line)
    unverified_claims(...)    file names/paths an agent's report mentions that no evidence backs
    apply_claim_check(...)    -> limitations "Unverified: <item> (no system record)" + LOW confidence
    mission_deliverables(...) every deliverable of a mission's agent reports (for the MissionReport)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePath

from ..core.models import Attachment, Confidence, Evidence
from ..core.store import WorldStore

# file extensions that make a token look like a file name in a report
FILE_EXTS = (
    "pdf", "xlsx", "xlsm", "xls", "csv", "tsv", "docx", "doc", "pptx", "ppt", "md", "txt", "json", "xml",
    "yaml", "yml", "html", "htm", "log", "zip", "png", "jpg", "jpeg", "gif", "sql", "rtf", "odt", "ods",
    "odp", "py", "js", "ts", "ipynb", "pages", "numbers", "key",
)
_FILE_RE = re.compile(
    r"(?<![\w@])((?:[A-Za-z]:)?[\w\-.~/\\()]*?[\w\-)]\.(?:" + "|".join(FILE_EXTS) + r"))(?![\w/])",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
_VERBS = {
    "file_listed": "listed", "file_read": "read", "file_written": "wrote", "web_search": "searched the web ·",
    "web_fetch": "fetched", "consult": "consulted", "approval": "requested approval ·",
}


def _base(ref: str) -> str:
    s = ref.replace("\\", "/").rstrip("/")
    return s.rsplit("/", 1)[-1] or s


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def record(store: WorldStore, *, mission_id: str, task_id: str | None, agent_id: str, kind: str,
                 ref: str, detail: str = "", ok: bool = True, summary: str | None = None) -> Evidence:
    """Record one action. The feed line names files by their base name ('SOFIA read Rocks_Q3.xlsx')."""
    evidence = Evidence(mission_id=mission_id, task_id=task_id, agent_id=agent_id, kind=kind,  # type: ignore[arg-type]
                        ref=ref, detail=detail, ok=ok)
    if summary is None:
        name = store.registry.get(agent_id).name if agent_id != "human" else "Human"
        shown = _base(ref) if kind.startswith("file_") and ref and not ref.startswith("(") else ref
        if kind == "consult":
            shown = store.registry.get(ref).name if ref in {a.id for a in store.registry.all()} else ref
        summary = _short(f"{name} {_VERBS.get(kind, kind)} {shown}", 140)
        if not ok:
            summary += f" · failed: {_short(detail, 80)}" if detail else " (failed)"
    return await store.record_evidence(evidence, summary=summary)


# ---------------------------------------------------------------------------
# Claim check
# ---------------------------------------------------------------------------


def file_mentions(text: str) -> list[str]:
    """File names / paths mentioned in free text (URLs are ignored)."""
    text = _URL_RE.sub(" ", text)
    out: list[str] = []
    for m in _FILE_RE.finditer(text):
        token = m.group(1).strip("()")
        if token and token not in out:
            out.append(token)
    return out


def _backed(mention: str, evidence: Iterable[Evidence]) -> bool:
    base = PurePath(mention.replace("\\", "/")).name.lower()
    return any(e.ok and base and base in f"{e.ref} {e.detail}".lower() for e in evidence)


def unverified_claims(actions_taken: list[str], inputs_used: list[str], *, task_evidence: list[Evidence],
                      mission_evidence: list[Evidence], provided: str = "") -> list[str]:
    """Files named in actions_taken without a record of THIS task, or in inputs_used without any record in
    the mission (inputs may come from dependency reports, whose files other tasks read or wrote). Files of the
    node context the system gave the agent (`provided`) count as backed."""
    given = provided.lower()

    def ok(mention: str, evidence: list[Evidence]) -> bool:
        base = PurePath(mention.replace("\\", "/")).name.lower()
        return _backed(mention, evidence) or (bool(base) and base in given)

    out: list[str] = []
    for line in actions_taken:
        out += [m for m in file_mentions(line) if not ok(m, task_evidence) and m not in out]
    for line in inputs_used:
        out += [m for m in file_mentions(line) if not ok(m, mission_evidence) and m not in out]
    return out


def apply_claim_check(actions_taken: list[str], inputs_used: list[str], limitations: list[str],
                      confidence: Confidence | str, *, task_evidence: list[Evidence],
                      mission_evidence: list[Evidence], provided: str = "") -> tuple[list[str], Confidence | str]:
    """Add 'Unverified: <item> (no system record)' limitations and drop confidence to LOW when needed."""
    bad = unverified_claims(actions_taken, inputs_used, task_evidence=task_evidence,
                            mission_evidence=mission_evidence, provided=provided)
    if not bad:
        return limitations, confidence
    return [*limitations, *(f"Unverified: {b} (no system record)" for b in bad)], Confidence.LOW


# ---------------------------------------------------------------------------
# Deliverables
# ---------------------------------------------------------------------------


def mission_deliverables(store: WorldStore, mission_id: str) -> list[Attachment]:
    """Every deliverable written in the mission (all rounds), in report order, without duplicates."""
    out: list[Attachment] = []
    seen: set[str] = set()
    for report in store.reports_for(mission_id):
        for att in report.deliverables:
            if att.id not in seen:
                seen.add(att.id)
                out.append(att)
    return out
