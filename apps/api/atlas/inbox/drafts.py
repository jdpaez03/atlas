"""Email drafts: the .eml export and the approve / discard decision (docs/INBOX.md §2).

ATLAS never sends email. An approved draft goes to Outlook's Drafts folder when the mail source can do that
(`export="outlook_drafts"`, `download_url` = the Outlook web link); otherwise it becomes
`<ATLAS_LOCAL_DIR>/outputs/corporate/inbox/<draft id>.eml` with `X-Unsent: 1`, which Outlook opens as an
editable, unsent message (`export="eml"`, `download_url=/drafts/{id}/eml`).
"""

from __future__ import annotations

import logging
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from ..core import paths
from ..core.models import EmailDraft

log = logging.getLogger("atlas.inbox")


def eml_path(draft_id: str, node: str = "corporate") -> Path:
    return paths.local_dir() / "outputs" / paths.safe_name(node) / "inbox" / f"{paths.safe_name(draft_id)}.eml"


def _msgid(value: str | None) -> str | None:
    """An RFC 5322 message id ('<...@...>') or None (Graph ids are not message ids)."""
    if not value:
        return None
    v = value.strip()
    if not v.startswith("<"):
        v = f"<{v}>"
    return v if "@" in v and v.endswith(">") and " " not in v else None


def _set_addresses(msg: EmailMessage, header: str, values: list[str]) -> None:
    values = [v.strip() for v in values if v and v.strip()]
    if not values:
        return
    try:
        msg[header] = ", ".join(values)
    except (ValueError, IndexError, TypeError):  # unparsable address text: keep it raw
        msg[header] = ", ".join(v.replace("\n", " ") for v in values)


def build_eml(draft: EmailDraft, *, in_reply_to: str | None = None, references: str | None = None) -> bytes:
    """A UTF-8 unsent message: To/Cc/Subject, In-Reply-To/References when the source id is known."""
    msg = EmailMessage(policy=policy.SMTP)  # CRLF line endings (Outlook on Windows)
    _set_addresses(msg, "To", draft.to)
    _set_addresses(msg, "Cc", draft.cc)
    msg["Subject"] = draft.subject.replace("\r", " ").replace("\n", " ")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="atlas.local")
    msg["X-Unsent"] = "1"
    parent = _msgid(in_reply_to)
    if parent:
        msg["In-Reply-To"] = parent
        msg["References"] = _msgid(references) or parent
    msg.set_content(draft.body, charset="utf-8")
    return msg.as_bytes()


def write_eml(draft: EmailDraft, *, in_reply_to: str | None = None) -> Path:
    path = eml_path(draft.id, draft.node)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(build_eml(draft, in_reply_to=in_reply_to))
    tmp.replace(path)
    return path


def download_name(draft: EmailDraft) -> str:
    base = paths.safe_name(draft.subject or "draft").strip() or "draft"
    return f"{base[:80]}.eml"
