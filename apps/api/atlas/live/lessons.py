"""Lessons: durable preferences the human teaches each agent (docs/LESSONS.md).

The human writes a comment for an agent in the Lessons tray ("the deck tables are too long"). ATLAS turns it into
one to three general, actionable rules and proposes them. The human approves, edits or discards them, or writes a
rule directly. Active lessons are appended to that agent's instructions in every future run: missions, briefs,
the digest, drafts, audits and SCRIBE's documents.

Source of truth: <ATLAS_LOCAL_DIR>/lessons.json (private, hand-editable, survives a history reset). The store
mirrors it for the UI (`lesson.upserted` events).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core import paths
from ..core.models import Lesson
from ..core.store import StoreError, WorldStore
from .llm import LLMError, Meter

log = logging.getLogger("atlas.lessons")

MAX_ACTIVE_PER_AGENT = 40
MAX_RULE_CHARS = 400

LESSONS_HEADER = (
    "# Lessons from the human (approved preferences)\n"
    "Follow these in this run. They refine how you work and present results; they never override the rules on "
    "evidence, sources and honesty. If one does not apply to this task, ignore it."
)


def lessons_path() -> Path:
    return paths.local_dir() / "lessons.json"


def load_file(path: Path | None = None) -> list[Lesson]:
    p = path or lessons_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("lessons file unreadable: %s", p)
        return []
    out = []
    for item in raw.get("lessons", []) if isinstance(raw, dict) else []:
        try:
            out.append(Lesson.model_validate(item))
        except ValueError:
            log.warning("skipping an invalid lesson in %s", p)
    return out


def save_file(lessons: list[Lesson], path: Path | None = None) -> None:
    p = path or lessons_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"lessons": [x.model_dump(mode="json") for x in lessons]}
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".lessons-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Injection into prompts
# ---------------------------------------------------------------------------


def active_lessons(store: Any, agent_id: str, node: str | None = None) -> list[Lesson]:
    items = getattr(getattr(store, "_state", None), "lessons", None) or []
    return [x for x in items if x.status == "active" and x.agent_id in (agent_id, "*")
            and (x.node is None or node is None or x.node == node)][:MAX_ACTIVE_PER_AGENT]


def lessons_block(store: Any, agent_id: str, node: str | None = None) -> str:
    items = active_lessons(store, agent_id, node)
    if not items:
        return ""
    return LESSONS_HEADER + "\n" + "\n".join(f"- {x.text}" for x in items)


def with_lessons(system: list[str], store: Any, agent_id: str, node: str | None = None) -> list[str]:
    """`system` plus this agent's active lessons (as the last block, so they refine what comes before)."""
    block = lessons_block(store, agent_id, node)
    return [*system, block] if block else list(system)


# ---------------------------------------------------------------------------
# Comment → proposed rules
# ---------------------------------------------------------------------------

PROPOSE_LESSONS_TOOL: dict[str, Any] = {
    "name": "propose_lessons",
    "description": "Turn the human's comment into durable rules for the agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rules": {"type": "array", "items": {"type": "string"},
                      "description": "1-3 rules, each one instruction to the agent"},
            "replaces": {"type": "array", "items": {"type": "string"},
                         "description": "ids of existing lessons these rules supersede (contradict or refine)"},
        },
        "required": ["rules"],
    },
}

DISTILL_SYSTEM = """You turn a human's comment about an AI agent's work into durable rules for that agent.

- Each rule is ONE instruction the agent can follow in future runs, written to the agent in the imperative,
  in the language of the comment ("Limita las tablas del deck a 8 filas; el resto va al anexo del PDF").
- Generalize the comment just enough to apply next time, never beyond what the human said. Keep their
  specifics (numbers, names, formats). If the comment only makes sense for one case, keep that condition in the
  rule ("En estudios de mercado, …").
- 1 rule is typical; use 2-3 only when the comment clearly contains separate preferences.
- If an existing lesson says the same thing, refine it and list its id in `replaces`; if the comment contradicts
  one, the new rule wins and the old id goes in `replaces`.
- Never weaken honesty or evidence rules (sources, labelling estimates, flagging untraced figures) even if asked.
Call propose_lessons once."""


class _NullMeter(Meter):
    """Lessons are drafted outside any mission: the call runs, its usage is not attributed to a mission."""

    async def record(self, model: str, usage: Any) -> None:
        return None

    async def record_totals(self, model: str, **kw: Any) -> None:  # type: ignore[override]
        return None


def distill_message(agent_line: str, comment: str, existing: list[Lesson]) -> str:
    lines = [f"Agent: {agent_line}", "", "Human's comment:", comment.strip(), ""]
    if existing:
        lines.append("This agent's current lessons:")
        lines += [f"- [{x.id}] {x.text}" for x in existing]
    else:
        lines.append("This agent has no lessons yet.")
    return "\n".join(lines)


async def distill(engine: Any, store: WorldStore, agent_id: str, comment: str) -> tuple[list[str], list[str]]:
    """(rules, replaced lesson ids). Without a live backend (or on failure), the comment itself is the rule."""
    fallback = ([" ".join(comment.split())[:MAX_RULE_CHARS]], [])
    info = engine.backend_info() if engine is not None else None
    if info is None or not info.backend:
        return fallback
    backend = info.backend
    existing = active_lessons(store, agent_id)
    if agent_id == "*":
        agent_line = "every ATLAS agent"
    else:
        a = store.registry.get(agent_id)
        agent_line = f"{a.name} — {a.title}: {a.description}"
    cfg = engine.config_for(backend)
    meter = _NullMeter(engine.llm if backend == "api" else None, store, "", engine.prices)
    scope = SimpleNamespace(meter=meter, store=store, mission_id=None, config=cfg)
    try:
        data = await engine.executor(backend).structured(
            scope, model=cfg.models.fast, system=[DISTILL_SYSTEM], prompt=distill_message(agent_line, comment, existing),
            tool=PROPOSE_LESSONS_TOOL, max_tokens=1200,
        )
    except LLMError as exc:
        log.warning("lesson distillation failed, keeping the comment as the rule: %s", exc)
        return fallback
    rules = [" ".join(str(r).split())[:MAX_RULE_CHARS] for r in (data or {}).get("rules") or [] if str(r).strip()]
    known = {x.id for x in existing}
    replaces = [str(r) for r in (data or {}).get("replaces") or [] if str(r) in known]
    return (rules[:3] or fallback[0]), replaces


# ---------------------------------------------------------------------------
# The book (file + store mirror)
# ---------------------------------------------------------------------------


class LessonBook:
    def __init__(self, store: WorldStore, path: Path | None = None):
        self.store = store
        self.path = path
        store.load_lessons(load_file(path))

    def _save(self) -> None:
        save_file(self.store.lessons(), self.path)

    async def _put(self, lesson: Lesson, summary: str) -> Lesson:
        lesson = await self.store.upsert_lesson(lesson, summary)
        self._save()
        return lesson

    def _name(self, agent_id: str) -> str:
        return "every agent" if agent_id == "*" else self.store.registry.get(agent_id).name

    async def comment(self, engine: Any, agent_id: str, comment: str, *, node: str | None = None) -> list[Lesson]:
        """Proposed lessons from a comment (they wait for approval)."""
        rules, replaces = await distill(engine, self.store, agent_id, comment)
        out = []
        for rule in rules:
            lesson = Lesson(agent_id=agent_id, text=rule, comment=comment.strip(), origin="comment", node=node,
                            replaces=replaces)
            out.append(await self._put(lesson, f"Lesson proposed for {self._name(agent_id)} · {rule[:100]}"))
        return out

    async def direct(self, agent_id: str, text: str, *, node: str | None = None) -> Lesson:
        """A rule the human wrote: active right away (it is already their decision)."""
        rule = " ".join(text.split())[:MAX_RULE_CHARS]
        lesson = Lesson(agent_id=agent_id, text=rule, comment=text.strip(), origin="direct", status="active",
                        node=node, decided_at=datetime.now(UTC))
        return await self._put(lesson, f"Lesson added for {self._name(agent_id)} · {rule[:100]}")

    async def decide(self, lesson_id: str, *, status: str | None = None, text: str | None = None) -> Lesson:
        lesson = self.store.lesson(lesson_id)
        update: dict[str, Any] = {}
        if text is not None and " ".join(text.split()):
            update["text"] = " ".join(text.split())[:MAX_RULE_CHARS]
        if status is not None:
            if status not in ("active", "dismissed", "retired", "proposed"):
                raise ValueError(f"invalid status '{status}'")
            update["status"] = status
            update["decided_at"] = datetime.now(UTC)
        lesson = lesson.model_copy(update=update)
        verb = {"active": "approved", "dismissed": "dismissed", "retired": "retired"}.get(status or "", "edited")
        out = await self._put(lesson, f"Lesson {verb} for {self._name(lesson.agent_id)} · {lesson.text[:100]}")
        if status == "active":
            for old_id in lesson.replaces:
                try:
                    old = self.store.lesson(old_id)
                except StoreError:  # a missing old lesson is nothing to retire
                    continue
                if old.status == "active":
                    await self._put(old.model_copy(update={"status": "retired", "decided_at": datetime.now(UTC)}),
                                    f"Lesson retired (replaced) for {self._name(old.agent_id)} · {old.text[:80]}")
        return out
