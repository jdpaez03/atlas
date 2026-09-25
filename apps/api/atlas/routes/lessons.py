"""Lessons tray (docs/LESSONS.md): comments → proposed rules → approved lessons that shape each agent."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.models import Lesson
from ..core.registry import RegistryError
from ..core.store import StoreError
from ..live.lessons import LessonBook
from .deps import http_error

router = APIRouter(tags=["lessons"])


class LessonCreate(BaseModel):
    agent_id: str = Field(description="agent id, or '*' for every agent")
    text: str = Field(min_length=1, max_length=4000)
    mode: Literal["comment", "direct"] = Field(
        default="comment", description="comment: ATLAS proposes rules for approval · direct: this text IS the rule")
    node: str | None = None


class LessonUpdate(BaseModel):
    status: Literal["active", "dismissed", "retired", "proposed"] | None = None
    text: str | None = Field(default=None, max_length=4000)


def _book(request: Request) -> LessonBook:
    return request.app.state.lessons


@router.get("/lessons", response_model=list[Lesson])
def list_lessons(request: Request, agent_id: str | None = None, status: str | None = None) -> list[Lesson]:
    items = _book(request).store.lessons(agent_id=agent_id, status=status)
    return sorted(items, key=lambda x: x.created_at, reverse=True)


@router.post("/lessons", response_model=list[Lesson])
async def create_lesson(body: LessonCreate, request: Request) -> list[Lesson]:
    book = _book(request)
    try:
        if body.agent_id != "*":
            book.store.registry.get(body.agent_id)
        if body.mode == "direct":
            return [await book.direct(body.agent_id, body.text, node=body.node)]
        return await book.comment(request.app.state.live, body.agent_id, body.text, node=body.node)
    except StoreError as exc:
        raise http_error(exc) from exc
    except RegistryError:
        raise HTTPException(404, f"unknown agent '{body.agent_id}'") from None


@router.patch("/lessons/{lesson_id}", response_model=Lesson)
async def update_lesson(lesson_id: str, body: LessonUpdate, request: Request) -> Lesson:
    try:
        return await _book(request).decide(lesson_id, status=body.status, text=body.text)
    except StoreError as exc:
        raise http_error(exc) from exc
