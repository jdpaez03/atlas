"""Inbox: follow-ups from work email (docs/INBOX.md).

  GET   /inbox/status                    InboxStatus
  POST  /inbox/scan                      run a scan now -> Mission (409 no/unconnected source or a scan running,
                                         422 no LLM backend)
  POST  /inbox/connect                   Microsoft sign-in (device code) -> {user_code, verification_uri,
                                         expires_in, message} (409 when the source has no sign-in)
  GET   /inbox/connect/status            {state: idle|pending|connected|failed, flow?, account?, error?, hint?,
                                         read_only}
  GET   /followups?status=&kind=         FollowUp[] (open first, then by due date)
  PATCH /followups/{id}                  {status?, due?, title?, priority?} -> FollowUp (due: null clears it)
  POST  /followups/{id}/draft            ALFRED drafts now (one-shot) -> EmailDraft
  GET   /drafts?status=&followup_id=     EmailDraft[] (newest first)
  POST  /drafts/{id}/decision            {decision: APPROVED|DISCARDED, subject?, body?, to?, cc?} -> EmailDraft
  GET   /drafts/{id}/eml                 the exported .eml (message/rfc822)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core.models import EmailDraft, FollowUp, Mission, Priority
from ..core.store import StoreError
from ..inbox.drafts import download_name, eml_path
from ..inbox.engine import (
    DraftConflictError,
    InboxEngine,
    InboxError,
    LiveUnavailableError,
    NoSourceError,
    ScanBusyError,
)
from ..live.llm import LLMError
from .deps import http_error

router = APIRouter(tags=["inbox"])


def get_inbox(request: Request) -> InboxEngine:
    return request.app.state.inbox


class InboxStatus(BaseModel):
    source: str | None  # "graph" | "folder" | None (not configured)
    connected: bool
    account: str | None
    detail: str
    hint: str | None
    can_draft: bool  # approved drafts go to Outlook Drafts (else .eml)
    pending: dict[str, Any] | None  # device-code sign-in in progress
    last_scan: datetime | None
    next_scan: datetime | None
    schedule: list[str]  # ["08:00", "15:00"] local (America/Mexico_City), weekdays
    processed_count: int
    scanning: bool
    scan_mission_id: str | None


class FollowUpPatch(BaseModel):
    status: Literal["OPEN", "WAITING", "DONE", "DISMISSED"] | None = None
    due: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    priority: Priority | None = None


class DraftDecision(BaseModel):
    decision: Literal["APPROVED", "DISCARDED"]
    subject: str | None = None
    body: str | None = None
    to: list[str] | None = None
    cc: list[str] | None = None


def _inbox_error(exc: InboxError) -> HTTPException:
    detail = str(exc) + (f" — {exc.hint}" if exc.hint else "")
    if isinstance(exc, (NoSourceError, ScanBusyError, DraftConflictError)):
        return HTTPException(409, detail)
    if isinstance(exc, LiveUnavailableError):
        return HTTPException(422, detail)
    return HTTPException(400, detail)


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


@router.get("/inbox/status", response_model=InboxStatus)
async def inbox_status(request: Request) -> dict[str, Any]:
    return await get_inbox(request).status()


@router.post("/inbox/scan", response_model=Mission)
async def inbox_scan(request: Request) -> Mission:
    try:
        return await get_inbox(request).start_scan(trigger="manual")
    except InboxError as exc:
        raise _inbox_error(exc) from exc


def _signin_source(inbox: InboxEngine) -> Any:
    src = inbox.source
    if src is None:
        raise _inbox_error(NoSourceError("No mail source is configured", NO_SOURCE))
    if not hasattr(src, "start_device_flow"):
        raise HTTPException(409, f"The {src.name} source does not need a sign-in")
    return src


NO_SOURCE = "set ATLAS_MS_CLIENT_ID in .env to connect Outlook / Microsoft 365 (docs/INBOX_SETUP.md)"


@router.post("/inbox/connect")
async def inbox_connect(request: Request) -> dict[str, Any]:
    src = _signin_source(get_inbox(request))
    try:
        return await src.start_device_flow()
    except Exception as exc:
        hint = getattr(exc, "hint", None)
        raise HTTPException(502, f"{exc}" + (f" — {hint}" if hint else "")) from exc


@router.get("/inbox/connect/status")
async def inbox_connect_status(request: Request) -> dict[str, Any]:
    return await _signin_source(get_inbox(request)).poll_device_flow()


# ---------------------------------------------------------------------------
# follow-ups
# ---------------------------------------------------------------------------

_STATUS_ORDER = {"WAITING": 0, "OPEN": 1, "DONE": 2, "DISMISSED": 3}


@router.get("/followups", response_model=list[FollowUp])
def list_followups(request: Request, status: str | None = None, kind: str | None = None) -> list[FollowUp]:
    items = get_inbox(request).store.followups(status=status, kind=kind)
    return sorted(items, key=lambda f: (_STATUS_ORDER.get(f.status, 9), f.due is None, f.due or date.max,
                                        -f.created_at.timestamp()))


@router.patch("/followups/{followup_id}", response_model=FollowUp)
async def patch_followup(followup_id: str, body: FollowUpPatch, request: Request) -> FollowUp:
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    if "priority" in changes and changes["priority"] is not None:
        changes["priority"] = Priority(changes["priority"]).value
    for k in ("status", "title", "priority"):
        if k in changes and changes[k] is None:
            changes.pop(k)
    try:
        return await get_inbox(request).patch_followup(followup_id, changes)
    except StoreError as exc:
        raise http_error(exc) from exc


@router.post("/followups/{followup_id}/draft", response_model=EmailDraft)
async def draft_followup(followup_id: str, request: Request) -> EmailDraft:
    try:
        return await get_inbox(request).draft_now(followup_id)
    except InboxError as exc:
        raise _inbox_error(exc) from exc
    except StoreError as exc:
        raise http_error(exc) from exc
    except LLMError as exc:
        raise HTTPException(502, f"ALFRED could not write the draft: {exc}") from exc


# ---------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------


@router.get("/drafts", response_model=list[EmailDraft])
def list_drafts(request: Request, status: str | None = None, followup_id: str | None = None) -> list[EmailDraft]:
    items = get_inbox(request).store.drafts(status=status, followup_id=followup_id)
    return sorted(items, key=lambda d: d.created_at, reverse=True)


@router.post("/drafts/{draft_id}/decision", response_model=EmailDraft)
async def decide_draft(draft_id: str, body: DraftDecision, request: Request) -> EmailDraft:
    try:
        return await get_inbox(request).decide_draft(
            draft_id, body.decision, subject=body.subject, body=body.body, to=body.to, cc=body.cc)
    except InboxError as exc:
        raise _inbox_error(exc) from exc
    except StoreError as exc:
        raise http_error(exc) from exc


@router.get("/drafts/{draft_id}/eml")
def download_eml(draft_id: str, request: Request) -> FileResponse:
    store = get_inbox(request).store
    try:
        draft = store.draft(draft_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    path = eml_path(draft.id, draft.node)
    if draft.export != "eml" or not path.is_file():
        raise HTTPException(404, "this draft has no .eml export (approve it first)")
    return FileResponse(path, media_type="message/rfc822", filename=download_name(draft))
