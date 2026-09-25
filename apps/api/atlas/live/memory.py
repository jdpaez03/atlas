"""Corporate memory (docs/MEMORY.md): what ATLAS knows from earlier work, and what the human told it.

Two sources, both per node:

- **History** (derived, nothing extra stored): every closed mission's latest report, the L10 briefs and the CC
  digests. A new mission, a follow-up round and an "Ask ATLAS" question get the most relevant entries (lexical
  search with a recency boost), labelled as prior knowledge: context to build on, not evidence. Figures must be
  re-verified or labelled ASSUMPTION, and AUDITOR checks it.
- **Knowledge notes**: facts the human states or approves ("Josué lleva Balcones desde septiembre"). They live in
  <ATLAS_LOCAL_DIR>/knowledge.json, are dated, and are always given to the orchestrator and the agents of that
  node.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core import paths
from ..core.models import KnowledgeNote

log = logging.getLogger("atlas.memory")

MAX_BLOCK_CHARS = 7000
MAX_NOTES = 40
TOP_MISSIONS = 5
TOP_OTHER = 2
HALF_LIFE_DAYS = 120
# Runs ATLAS starts itself: their results are indexed as the brief / digest they produce, not as missions.
SYSTEM_RUNS = ("Inbox scan ·", "CC digest ·", "ARGOS watch ·", "L10 brief ·")

_STOP_WORDS = """
a al algo ante antes aqui asi aun como con contra cual cuales cuando de del desde donde dos el ella
ellas ellos en entre era es esa ese eso esta estan este esto estos fue fueron ha hace hacer han
hasta hay la las le les lo los mas me mi mis muy nada ni no nos o otra otro para pero poco por
porque que quien se sea segun ser si sin sobre son su sus tambien tan te tiene todo todos tu un una
uno unos y ya yo ver vez hoy quiero dame haz favor sobre cual cuales the and for with from that
this what which who when where how are was were be been has have had not you your our into about
can could would should will need needs make made do does did of to in on at by it its as an or if
mision misiones mission missions atlas
"""
_STOP = frozenset(_STOP_WORDS.split())
_TOKEN = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    """Lowercase without accents (so 'Amāra', 'amara' and 'AMARA' match)."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(fold(text)) if (len(t) > 2 or t.isdigit()) and t not in _STOP]


def _date(d: datetime | None) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%d") if d else "?"


def _age_days(d: datetime | None, now: datetime) -> float:
    if d is None:
        return 365.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return max(0.0, (now - d).total_seconds() / 86400)


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Entries (derived from the history)
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    id: str  # mission / brief / digest id
    kind: str  # mission | brief | digest
    node: str
    title: str
    date: datetime | None
    summary: str
    points: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    status: str = ""
    documents: list[str] = field(default_factory=list)
    score: float = 0.0

    def fields(self) -> list[tuple[str, float]]:
        return [(self.title, 3.0), (" ".join(self.topics), 3.0), (self.summary, 1.5), (" ".join(self.points), 1.0)]

    def render(self) -> str:
        label = {"mission": "Mission", "brief": "L10 brief", "digest": "CC digest"}[self.kind]
        head = f"### [{self.id}] {_date(self.date)} · {label} · {_short(self.title, 160)}"
        if self.status:
            head += f" · {self.status}"
        lines = [head]
        if self.topics:
            lines.append("Topics: " + ", ".join(self.topics[:8]))
        if self.summary:
            lines.append(_short(self.summary, 700))
        lines += [f"- {_short(p, 240)}" for p in self.points[:8]]
        if self.documents:
            lines.append("Documents: " + ", ".join(self.documents[:4]))
        return "\n".join(lines)


def history_entries(store: Any, node: str, *, exclude: set[str] | None = None) -> list[Entry]:
    exclude = exclude or set()
    state = store.snapshot() if hasattr(store, "snapshot") else store
    out: list[Entry] = []
    latest: dict[str, Any] = {}
    for r in state.mission_reports:
        if r.mission_id not in latest or r.version >= latest[r.mission_id].version:
            latest[r.mission_id] = r
    for m in state.missions:
        if (m.node != node or m.id in exclude or m.mode != "live" or m.id not in latest
                or m.objective.startswith(SYSTEM_RUNS)):
            continue
        r = latest[m.id]
        points = [f"[{c.kind}] {c.statement}" for c in r.key_findings[:6]]
        points += [f"Next: {a}" for a in r.next_actions[:4]]
        points += [f"Open: {a}" for a in r.needs_human_attention[:2]]
        out.append(Entry(
            id=m.id, kind="mission", node=node, title=m.objective, date=r.created_at or m.created_at,
            summary=r.executive_summary, points=points, topics=list(getattr(r, "topics", []) or []),
            status=str(r.objective_status), documents=[d.name for d in (getattr(r, "documents", None) or [])],
        ))
    for b in getattr(state, "briefs", []) or []:
        if b.node != node or b.id in exclude:
            continue
        pts = [x for lines in b.sections.values() for x in lines[:3]]
        out.append(Entry(id=b.id, kind="brief", node=node, title=f"L10 brief {b.week}", date=b.created_at,
                         summary=" ".join(b.headline), points=pts[:8]))
    for d in getattr(state, "digests", []) or []:
        if d.node != node or d.id in exclude:
            continue
        pts = [f"{t.subject}: {' '.join(t.summary[:2])}" for t in d.threads[:8]]
        out.append(Entry(id=d.id, kind="digest", node=node, title="CC digest", date=d.window_end or d.created_at,
                         summary=" ".join(d.headline), points=pts, topics=[t.project for t in d.threads if t.project]))
    return out


def search(entries: list[Entry], query: str, *, now: datetime | None = None, top_missions: int = TOP_MISSIONS,
           top_other: int = TOP_OTHER) -> list[Entry]:
    """BM25-style lexical match (title and topics weigh more) × a recency boost. Entries sharing no meaningful
    word with the query are never returned."""
    q = set(tokens(query))
    if not q or not entries:
        return []
    now = now or datetime.now(UTC)
    docs = []
    for e in entries:
        tf: dict[str, float] = {}
        length = 0
        for text, w in e.fields():
            for t in tokens(text):
                tf[t] = tf.get(t, 0.0) + w
                length += 1
        docs.append((e, tf, max(length, 1)))
    avg = sum(n for _, _, n in docs) / len(docs)
    df = {t: sum(1 for _, tf, _ in docs if t in tf) for t in q}
    n_docs = len(docs)
    scored = []
    for e, tf, length in docs:
        s = 0.0
        hits = 0
        for t in q:
            f = tf.get(t, 0.0)
            if not f:
                continue
            hits += 1
            idf = math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / avg))
        if not hits:
            continue
        recency = 0.5 + 0.5 * math.pow(0.5, _age_days(e.date, now) / HALF_LIFE_DAYS)
        e.score = round(s * recency, 4)
        scored.append(e)
    scored.sort(key=lambda e: e.score, reverse=True)
    if not scored:
        return []
    floor = scored[0].score * 0.2  # drop the long tail of weak matches
    missions = [e for e in scored if e.kind == "mission" and e.score >= floor][:top_missions]
    other = [e for e in scored if e.kind != "mission" and e.score >= floor][:top_other]
    return sorted(missions + other, key=lambda e: e.score, reverse=True)


# ---------------------------------------------------------------------------
# Knowledge notes (file)
# ---------------------------------------------------------------------------


def knowledge_path() -> Path:
    return paths.local_dir() / "knowledge.json"


def load_notes(path: Path | None = None) -> list[KnowledgeNote]:
    p = path or knowledge_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("knowledge file unreadable: %s", p)
        return []
    out = []
    for item in raw.get("notes", []) if isinstance(raw, dict) else []:
        try:
            out.append(KnowledgeNote.model_validate(item))
        except ValueError:
            log.warning("skipping an invalid knowledge note")
    return out


def save_notes(notes: list[KnowledgeNote], path: Path | None = None) -> None:
    p = path or knowledge_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".knowledge-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"notes": [n.model_dump(mode="json") for n in notes]}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def active_notes(node: str) -> list[KnowledgeNote]:
    notes = [n for n in load_notes() if n.status == "active" and n.node == node]
    return sorted(notes, key=lambda n: n.created_at, reverse=True)[:MAX_NOTES]


def add_note(node: str, text: str, source: str = "direct") -> KnowledgeNote:
    notes = load_notes()
    note = KnowledgeNote(node=node, text=" ".join(text.split())[:600], source=source)
    notes.append(note)
    save_notes(notes)
    return note


def update_note(note_id: str, *, status: str | None = None, text: str | None = None) -> KnowledgeNote:
    notes = load_notes()
    for i, n in enumerate(notes):
        if n.id == note_id:
            upd: dict[str, Any] = {}
            if status is not None:
                upd["status"] = status
            if text is not None and text.strip():
                upd["text"] = " ".join(text.split())[:600]
            notes[i] = n.model_copy(update=upd)
            save_notes(notes)
            return notes[i]
    raise KeyError(note_id)


# ---------------------------------------------------------------------------
# The block given to ATLAS and the agents
# ---------------------------------------------------------------------------

MEMORY_HEADER = (
    "# Prior knowledge (earlier work in this node and what the human told ATLAS)\n"
    "Context to build on, not evidence. Build on it instead of redoing work, and say how this run relates to it "
    "(confirms, updates, contradicts). It may be out of date: re-verify any figure you rely on in this run; cite "
    "earlier work as \"Misión <id> (<date>)\"; anything you did not re-verify is an ASSUMPTION, never a FACT. "
    "Knowledge notes are the human's statements, dated: treat them as the current position unless newer "
    "evidence contradicts them (then flag the contradiction)."
)


def notes_text(notes: list[KnowledgeNote]) -> str:
    if not notes:
        return ""
    return "## Knowledge notes (from the human)\n" + "\n".join(
        f"- ({_date(n.created_at)}) {n.text}" for n in notes)


def memory_block(entries: list[Entry], notes: list[KnowledgeNote]) -> str:
    parts = [p for p in (notes_text(notes),) if p]
    if entries:
        parts.append("## Related earlier work\n" + "\n\n".join(e.render() for e in entries))
    if not parts:
        return ""
    text = MEMORY_HEADER + "\n\n" + "\n\n".join(parts)
    return text if len(text) <= MAX_BLOCK_CHARS else text[: MAX_BLOCK_CHARS - 1] + "…"


def recall(store: Any, node: str, query: str, *, exclude: set[str] | None = None) -> tuple[str, list[Entry]]:
    """(block for the prompt, entries used). Never raises: memory is best-effort context."""
    try:
        entries = search(history_entries(store, node, exclude=exclude), query)
        return memory_block(entries, active_notes(node)), entries
    except Exception:
        log.exception("memory recall failed")
        return "", []
