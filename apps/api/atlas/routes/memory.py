"""Corporate memory (docs/MEMORY.md): Ask ATLAS, knowledge notes and the memory index."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..core.models import AskMessage, KnowledgeNote, MemoryRef
from ..live import ask as ask_mod
from ..live import memory as memory_mod

router = APIRouter(tags=["memory"])


class AskBody(BaseModel):
    node: str = "corporate"
    question: str = Field(min_length=1, max_length=6000)


class NoteBody(BaseModel):
    node: str = "corporate"
    text: str = Field(min_length=1, max_length=2000)
    source: Literal["direct", "ask"] = "direct"


class NoteUpdate(BaseModel):
    status: Literal["active", "retired"] | None = None
    text: str | None = Field(default=None, max_length=2000)


def _check_node(request: Request, node: str) -> None:
    if node not in {n.id for n in request.app.state.registry.nodes()}:
        raise HTTPException(404, f"unknown node '{node}'")


@router.get("/ask", response_model=list[AskMessage])
def get_thread(request: Request, node: str = "corporate", limit: int = Query(default=100, ge=1, le=300)) -> list[AskMessage]:
    _check_node(request, node)
    return ask_mod.load_thread(node)[-limit:]


@router.post("/ask", response_model=list[AskMessage])
async def post_question(body: AskBody, request: Request) -> list[AskMessage]:
    _check_node(request, body.node)
    return await ask_mod.ask(request.app.state.live, request.app.state.store, body.node, body.question)


@router.post("/ask/clear")
def clear(request: Request, node: str = "corporate") -> dict[str, str]:
    _check_node(request, node)
    ask_mod.clear_thread(node)
    return {"status": "cleared"}


@router.get("/knowledge", response_model=list[KnowledgeNote])
def list_notes(request: Request, node: str = "corporate", status: str | None = None) -> list[KnowledgeNote]:
    _check_node(request, node)
    notes = [n for n in memory_mod.load_notes() if n.node == node and (status is None or n.status == status)]
    return sorted(notes, key=lambda n: n.created_at, reverse=True)


@router.post("/knowledge", response_model=KnowledgeNote)
def add_note(body: NoteBody, request: Request) -> KnowledgeNote:
    _check_node(request, body.node)
    return memory_mod.add_note(body.node, body.text, body.source)


@router.patch("/knowledge/{note_id}", response_model=KnowledgeNote)
def update_note(note_id: str, body: NoteUpdate) -> KnowledgeNote:
    try:
        return memory_mod.update_note(note_id, status=body.status, text=body.text)
    except KeyError:
        raise HTTPException(404, f"unknown note '{note_id}'") from None


@router.get("/memory", response_model=list[MemoryRef])
def memory_index(request: Request, node: str = "corporate", q: str | None = None,
                 limit: int = Query(default=50, ge=1, le=500)) -> list[MemoryRef]:
    """What ATLAS remembers in a node: every entry (newest first), or the best matches for `q`."""
    _check_node(request, node)
    entries = memory_mod.history_entries(request.app.state.store, node)
    if q:
        entries = memory_mod.search(entries, q, top_missions=limit, top_other=limit)
    else:
        entries.sort(key=lambda e: e.date.timestamp() if e.date else 0, reverse=True)
    return [MemoryRef(id=e.id, kind=e.kind, title=e.title[:200], date=e.date) for e in entries[:limit]]
