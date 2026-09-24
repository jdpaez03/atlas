"""POST /approvals/{id}/decision — human in the loop."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.models import ApprovalRequest
from ..core.store import StoreError
from .deps import StoreDep, http_error

router = APIRouter(tags=["approvals"])


class Decision(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    note: str | None = None


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalRequest)
async def decide(approval_id: str, body: Decision, store: StoreDep) -> ApprovalRequest:
    try:
        return await store.decide_approval(approval_id, body.decision, body.note)
    except StoreError as exc:
        raise http_error(exc) from exc
