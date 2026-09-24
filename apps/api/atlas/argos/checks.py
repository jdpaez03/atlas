"""Shared interface for ARGOS checks (docs/ARGOS.md). Written by the lead so builders can work in parallel.

A check inspects one source (dashboards, the L10 module, the Rocks file…) and returns AlertDrafts.
The engine (engine.py) turns drafts into Alerts: upsert by fingerprint, auto-resolve what a successful
run no longer sees, evidence, events.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..core.models import AlertEvidence

if TYPE_CHECKING:  # pragma: no cover
    from ..core.store import WorldStore
    from ..inbox.sources.base import MailSource
    from ..live.runtime import MissionScope

Severity = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass
class AlertDraft:
    check: str
    kind: str
    severity: Severity
    title: str
    fingerprint: str
    detail: str = ""
    project: str | None = None
    evidence: list[AlertEvidence] = field(default_factory=list)


@dataclass
class CheckResult:
    alerts: list[AlertDraft] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # human lines for the mission report
    ok: bool = True  # False → the engine does NOT auto-resolve this check's alerts


class CheckNotConfigured(RuntimeError):
    """Raised by a check whose configuration is missing; `hint` tells the user what to set."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.hint = hint


@dataclass
class CheckContext:
    store: WorldStore
    scope: MissionScope | None  # LLM steps (executor.structured) + evidence; None in pure unit tests
    mail: MailSource | None
    config: dict[str, Any]  # parsed watch.yaml (may be empty)
    now: datetime
    mission_id: str | None = None
    node: str = "corporate"


class Check(Protocol):
    name: str

    async def run(self, ctx: CheckContext) -> CheckResult: ...


def fingerprint(*parts: object) -> str:
    """Stable key: lowercase, accents stripped, whitespace/punctuation collapsed."""

    def norm(p: object) -> str:
        s = unicodedata.normalize("NFKD", str(p)).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")

    return ":".join(norm(p) for p in parts)
