"""Ask ATLAS (docs/MEMORY.md): a conversation with the orchestrator about the corporate world. It answers from
memory (earlier missions, briefs, digests, the human's knowledge notes) and the live state (running missions,
follow-ups, alerts, Rocks). It proposes facts to remember from what the human says, and suggests a mission when
a question needs new work. It never invents: what it doesn't know, it says.

The conversation is kept per node in <ATLAS_LOCAL_DIR>/ask/<node>.json.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core import paths
from ..core.models import AskMessage, MemoryRef
from .lessons import _NullMeter, with_lessons
from .llm import LLMError, current_agent
from .memory import Entry, active_notes, history_entries, notes_text, search

log = logging.getLogger("atlas.ask")

MAX_THREAD = 300
CONTEXT_TURNS = 8

ANSWER_TOOL: dict[str, Any] = {
    "name": "answer",
    "description": "Answer the human.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "the reply (markdown: short paragraphs, - bullets)"},
            "refs": {"type": "array", "items": {"type": "string"},
                     "description": "ids of the earlier missions / briefs / digests the answer relies on"},
            "remember": {"type": "array", "items": {"type": "string"},
                         "description": "durable facts the HUMAN stated in the latest message, one standalone "
                                        "sentence each (empty if none)"},
            "suggest_mission": {"type": "string",
                                "description": "if answering needs new research or analysis: the mission objective"},
        },
        "required": ["answer"],
    },
}

ASK_NOTE = """# Conversation mode (Ask ATLAS)
You are talking with the human directly, not running a mission. You are their chief of staff: you keep up with
everything done in this node and what they told you, and you answer like a colleague who was there.

- Answer from what you are given: the earlier work (with ids), the knowledge notes, the live state and the
  conversation. Cite the ids you relied on in `refs`. Mention how old information is when it matters
  ("según la misión del 10 de septiembre…").
- If the answer isn't in what you were given, say so plainly. If it needs research or analysis, propose a
  mission objective in `suggest_mission` (specific, one or two sentences). Never invent facts, figures or
  events.
- `remember`: when the human's latest message states something durable about their world (a decision, an
  owner, a date, a status, a preference about a project), write it as a dated, standalone sentence for the
  knowledge notes. Only what THEY said — never your own conclusions. Usually empty.
- Reply in the human's language, lead with the answer, keep it short (bullets for lists).
Call answer once."""


# ---------------------------------------------------------------------------
# Thread (file)
# ---------------------------------------------------------------------------


def thread_path(node: str) -> Path:
    return paths.local_dir() / "ask" / f"{paths.safe_name(node)}.json"


def load_thread(node: str) -> list[AskMessage]:
    p = thread_path(node)
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [AskMessage.model_validate(x) for x in raw.get("messages", [])]
    except (OSError, ValueError):
        log.exception("ask thread unreadable: %s", p)
        return []


def save_thread(node: str, messages: list[AskMessage]) -> None:
    p = thread_path(node)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ask-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"messages": [m.model_dump(mode="json") for m in messages[-MAX_THREAD:]]}, fh,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def clear_thread(node: str) -> None:
    """Start a new conversation (the old one is kept next to it, timestamped)."""
    p = thread_path(node)
    if p.is_file():
        p.rename(p.with_name(f"{p.stem}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _d(x: datetime | date | None) -> str:
    return x.strftime("%Y-%m-%d") if x else "?"


def state_text(store: Any, node: str, today: date) -> str:
    s = store.snapshot()
    lines: list[str] = [f"Today: {today.isoformat()}"]
    running = [m for m in s.missions if m.node == node and m.phase not in ("CLOSED",) and m.mode == "live"]
    if running:
        lines.append("Missions in progress:")
        lines += [f"- [{m.id}] {m.objective[:140]} · {m.phase}" for m in running[:6]]
    ups = [f for f in s.followups if f.status in ("OPEN", "WAITING")]
    if ups:
        overdue = sorted([f for f in ups if f.due and f.due < today], key=lambda f: f.due)
        lines.append(f"Follow-ups: {len(ups)} open, {len(overdue)} overdue.")
        lines += [f"- OVERDUE {f.title} · {f.counterpart or ''} · due {_d(f.due)}" for f in overdue[:8]]
        upcoming = sorted([f for f in ups if f.due and f.due >= today], key=lambda f: f.due)[:5]
        lines += [f"- due {_d(f.due)}: {f.title} · {f.counterpart or ''}" for f in upcoming]
    alerts = [a for a in s.alerts if a.status == "OPEN"]
    if alerts:
        lines.append(f"Open ARGOS alerts: {len(alerts)}.")
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        lines += [f"- {a.severity} · {a.project or ''} · {a.title}" for a in
                  sorted(alerts, key=lambda a: rank.get(str(a.severity), 3))[:8]]
    if s.rocks:
        by: dict[str, int] = {}
        for r in s.rocks:
            by[str(r.status)] = by.get(str(r.status), 0) + 1
        lines.append("Rocks: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
        lines += [f"- {r.status} · {r.title} ({r.owner}) · {r.reason}"[:200] for r in s.rocks
                  if str(r.status) in ("OFF_TRACK", "AT_RISK", "FAILED")][:8]
    briefs = sorted([b for b in s.briefs if b.node == node], key=lambda b: b.created_at, reverse=True)
    if briefs:
        lines.append(f"Latest L10 brief ({briefs[0].week}): " + " · ".join(briefs[0].headline))
    return "\n".join(lines)


def ask_message(question: str, history: list[AskMessage], entries: list[Entry], notes_block: str,
                state: str) -> str:
    parts = ["STAGE: ASK", "## Live state", state]
    if notes_block:
        parts.append(notes_block)
    parts.append("## Earlier work that may be relevant")
    parts.append("\n\n".join(e.render() for e in entries) if entries else "(nothing in the history matched)")
    turns = history[-CONTEXT_TURNS:]
    if turns:
        parts.append("## Conversation so far")
        parts += [f"[{'Human' if m.role == 'human' else 'ATLAS'}] {m.text[:1500]}" for m in turns]
    parts.append(f"## The human now says\n{question.strip()}")
    return "\n\n".join(parts)


def _refs(ids: list[str], entries: list[Entry]) -> list[MemoryRef]:
    by = {e.id: e for e in entries}
    out = []
    for i in ids:
        e = by.get(str(i).strip().strip("[]"))
        if e and all(r.id != e.id for r in out):
            out.append(MemoryRef(id=e.id, kind=e.kind, title=e.title[:200], date=e.date))
    return out


async def ask(engine: Any, store: Any, node: str, question: str, *, today: date | None = None) -> list[AskMessage]:
    """The human's turn and ATLAS's answer (both saved to the thread)."""
    from ..inbox.state import user_tz

    today = today or datetime.now(user_tz()).date()
    history = load_thread(node)
    human = AskMessage(node=node, role="human", text=question.strip())
    last_human = next((m.text for m in reversed(history) if m.role == "human"), "")
    entries = search(history_entries(store, node), f"{question} {last_human}", top_missions=6, top_other=3)
    notes_block = notes_text(active_notes(node))
    info = engine.backend_info() if engine is not None else None
    if info is None or not info.backend:
        text = ("Live agents are off (no Claude plan or API key), so I can't reason over this. "
                + ("Closest earlier work:\n" + "\n".join(f"- [{e.id}] {e.title}" for e in entries[:5])
                   if entries else "Nothing in the history matches."))
        answer = AskMessage(node=node, role="atlas", text=text, refs=_refs([e.id for e in entries[:5]], entries))
    else:
        backend = info.backend
        cfg = engine.config_for(backend)
        orchestrator = engine.loader_for(backend).resolve(store.registry.orchestrator.id)
        meter = _NullMeter(engine.llm if backend == "api" else None, store, "", engine.prices)
        scope = SimpleNamespace(meter=meter, store=store, mission_id=None, config=cfg)
        system = with_lessons([orchestrator.role_prompt, ASK_NOTE], store, orchestrator.id, node)
        token = current_agent.set(orchestrator.id)
        try:
            data = await engine.executor(backend).structured(
                scope, model=cfg.models.default, system=system,
                prompt=ask_message(question, history, entries, notes_block, state_text(store, node, today)),
                tool=ANSWER_TOOL, max_tokens=3000,
            )
        except LLMError as exc:
            data = {"answer": f"I couldn't answer right now: {exc}"}
        finally:
            current_agent.reset(token)
        data = data or {"answer": "(no answer)"}
        remember = [" ".join(str(x).split())[:600] for x in data.get("remember") or [] if str(x).strip()][:3]
        suggest = " ".join(str(data.get("suggest_mission") or "").split())[:600] or None
        answer = AskMessage(node=node, role="atlas", text=str(data.get("answer") or "").strip() or "(no answer)",
                            refs=_refs(list(data.get("refs") or []), entries), remember=remember,
                            suggest_mission=suggest)
    save_thread(node, [*history, human, answer])
    return [human, answer]
