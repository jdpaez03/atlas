"""Mail source contract (docs/INBOX.md §1). The inbox engine codes against these types only.

Privacy: sources return bodies on demand and never write them to disk or logs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from atlas.core.models import EmailDraft

PREVIEW_MAX = 255
DEFAULT_BODY_MAX_CHARS = 6000


@dataclass
class MailMessage:
    id: str  # stable id (Graph id, or file hash for folder source)
    internet_message_id: str | None
    conversation_id: str | None
    subject: str
    sender: str  # "Name <email>"
    to: list[str]
    cc: list[str]
    received_at: datetime  # timezone-aware (UTC when the source gives no zone)
    is_from_me: bool  # the user sent it (for AWAITING_REPLY / MY_COMMITMENT detection)
    web_link: str | None
    preview: str  # ≤ 255 chars
    has_attachments: bool = False  # the message carries file attachments (Graph `hasAttachments`)


@dataclass
class AttachmentMeta:
    """A file attached to a message (docs/ARGOS.md, dashboards check)."""

    id: str  # stable within its message (Graph attachment id, or an index for folder files)
    name: str
    size: int = 0  # bytes (0 when unknown)
    content_type: str = ""


@dataclass
class SourceStatus:
    name: str  # "graph" | "folder"
    connected: bool
    account: str | None = None  # the mailbox address, or the watched folder
    detail: str = ""  # what is going on, for the UI
    hint: str | None = None  # what the user should do next (plain language), when something is wrong
    can_draft: bool = False  # create_outlook_draft is supported right now
    pending: dict | None = field(default=None)  # a device-code flow in progress: {user_code, verification_uri, ...}


@runtime_checkable
class MailSource(Protocol):
    name: str  # "graph" | "folder"

    async def status(self) -> SourceStatus: ...

    async def list_messages(self, since: datetime, limit: int = 200) -> list[MailMessage]:
        """Inbox + sent items received/sent at or after `since`, newest first."""
        ...

    async def get_body(self, message_id: str) -> str:
        """Plain text (HTML → text), quoted history trimmed, capped at ATLAS_MAIL_BODY_MAX_CHARS."""
        ...

    async def create_outlook_draft(self, draft: EmailDraft) -> str | None:
        """Save the draft in Outlook's Drafts folder; returns its web link, or None if unsupported."""
        ...


@runtime_checkable
class AttachmentSource(MailSource, Protocol):
    """A MailSource that can also read attachments (Graph and folder sources). Kept separate from MailSource so
    sources without attachment support (and test fakes) still satisfy the base contract."""

    async def list_attachments(self, message_id: str) -> list[AttachmentMeta]:
        """The message's file attachments (inline images, attached items and references are skipped)."""
        ...

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """The attachment's bytes. Raises MailSourceError / KeyError when it can't be read."""
        ...


def supports_attachments(source: object) -> bool:
    return callable(getattr(source, "list_attachments", None)) and callable(
        getattr(source, "download_attachment", None))


class MailSourceError(RuntimeError):
    """A source failure with a plain-language hint for the user."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------

# A line that starts quoted history in a reply (English / Spanish Outlook, Gmail, Apple Mail).
_QUOTE_START = re.compile(
    r"""^\s*(?:
        -{2,}\s*(?:Original\s+Message|Mensaje\s+original|Forwarded\s+message|Mensaje\s+reenviado)\s*-{2,}
      | _{8,}                                   # Outlook's separator line
      | (?:From|De)\s*:\s*\S
      | On\s.+\bwrote:\s*$
      | El\s.+\bescribi[óo]:\s*$
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def body_max_chars() -> int:
    try:
        return max(200, int(os.getenv("ATLAS_MAIL_BODY_MAX_CHARS", DEFAULT_BODY_MAX_CHARS)))
    except ValueError:
        return DEFAULT_BODY_MAX_CHARS


def trim_quoted(text: str, max_chars: int | None = None) -> str:
    """Drop quoted history (everything from the first reply header on) and cap the length."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        if any(k.strip() for k in kept) and _QUOTE_START.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    cap = body_max_chars() if max_chars is None else max_chars
    if len(out) > cap:
        out = out[:cap].rstrip() + "\n[…truncated]"
    return out


def make_preview(text: str) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat if len(flat) <= PREVIEW_MAX else flat[: PREVIEW_MAX - 1].rstrip() + "…"


def format_address(name: str | None, address: str | None) -> str:
    name = (name or "").strip().strip('"')
    address = (address or "").strip()
    if name and address and name.lower() != address.lower():
        return f"{name} <{address}>"
    return address or name


def address_of(value: str) -> str:
    """The bare lower-cased email address from "Name <email>" or "email"."""
    m = re.search(r"<([^>]+)>", value or "")
    return (m.group(1) if m else (value or "")).strip().lower()


def my_addresses() -> set[str]:
    return {a.strip().lower() for a in os.getenv("ATLAS_MAIL_ME", "").split(";") if a.strip()}
