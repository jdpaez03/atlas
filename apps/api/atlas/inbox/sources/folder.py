"""FolderSource: the no-IT fallback (docs/INBOX.md §1, docs/INBOX_SETUP.md option B).

Reads mail files that something else drops into ATLAS_MAIL_FOLDER (a Power Automate flow writing .json into a
OneDrive folder, an Outlook rule, or messages saved by hand): `.eml`, `.msg` and `.json`. Files are only ever
read — never modified, moved or deleted. A file under a folder named like "sent" / "sentitems" / "Sent Items",
or sent from one of the ATLAS_MAIL_ME addresses, counts as sent by the user.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from atlas.core.models import EmailDraft

from .base import (
    MailMessage,
    SourceStatus,
    address_of,
    format_address,
    make_preview,
    my_addresses,
    trim_quoted,
)

log = logging.getLogger("atlas.inbox.folder")

SUFFIXES = {".eml", ".msg", ".json"}
MAX_FILE_BYTES = 25 * 1024 * 1024
_SENT_DIRS = {"sent", "sentitems", "sent items", "sent_items", "enviados", "elementos enviados"}


# ---------------------------------------------------------------------------
# HTML → text (stdlib only)
# ---------------------------------------------------------------------------

_LINE = {"div", "tr", "section", "article", "header", "footer", "pre"}  # break the line
_PARA = {"p", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}  # leave a blank line
_BLOCK = _LINE | _PARA
_SKIP = {"style", "script", "head", "title", "xml"}


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n________________\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in _BLOCK:
            self._break(tag)

    def _break(self, tag: str) -> None:
        want = 2 if tag in _PARA else 1
        if not "".join(self.parts).strip():
            return  # nothing written yet: no leading blank lines
        have = 0
        for part in reversed(self.parts):  # count trailing newlines, ignoring whitespace-only text between tags
            stripped = part.replace(" ", "").replace("\t", "")
            if stripped.strip("\n"):
                have += len(stripped) - len(stripped.rstrip("\n"))
                break
            have += stripped.count("\n")
        self.parts.append("\n" * max(0, want - have))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self._break(tag)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(re.sub(r"[ \t\r\n\f]+", " ", data))


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML: fall back to stripping tags
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [line.strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<(html|body|div|p|br|table|span)\b", text[:4000], re.IGNORECASE))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        pass
    try:
        return _aware(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError):
        return None


def _addr_list(value: Any) -> list[str]:
    """Recipients from "a@x; B <b@y>", a list of strings, or Graph-style {emailAddress:{name,address}} dicts."""
    if not value:
        return []
    if isinstance(value, str):
        items: list[Any] = [v for v in re.split(r"[;,]\s*(?![^<]*>)", value) if v.strip()]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            ea = item.get("emailAddress") or item
            formatted = format_address(ea.get("name") or ea.get("Name"), ea.get("address") or ea.get("Address"))
        else:
            pairs = getaddresses([str(item)])
            formatted = format_address(*pairs[0]) if pairs and (pairs[0][0] or pairs[0][1]) else str(item).strip()
        if formatted:
            out.append(formatted)
    return out


def _ci(data: dict, *keys: str) -> Any:
    """Case-insensitive lookup trying several names (PascalCase template or camelCase triggerBody())."""
    lowered = {k.lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
            return lowered[key.lower()]
    return None


def _file_id(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    stamp = f"{rel}|{path.stat().st_mtime_ns}"
    return "file-" + hashlib.sha1(stamp.encode("utf-8")).hexdigest()


def _in_sent_dir(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(p.strip().lower() in _SENT_DIRS for p in parts)


def _open_msg(path: Path):  # separated so tests can inject a fake (extract-msg cannot write .msg files)
    import extract_msg

    return extract_msg.openMsg(str(path))


class _Parsed:
    __slots__ = ("body", "msg")

    def __init__(self, msg: MailMessage, body: str):
        self.msg = msg
        self.body = body


class FolderSource:
    name = "folder"

    def __init__(self, folder: str | os.PathLike | None = None, me: set[str] | None = None):
        raw = folder if folder is not None else os.getenv("ATLAS_MAIL_FOLDER", "")
        self.root = Path(os.path.expanduser(str(raw))) if str(raw).strip() else None
        self.me = me if me is not None else my_addresses()
        self._paths: dict[str, Path] = {}  # message id -> file (in memory only)

    # -- MailSource -------------------------------------------------------------------------------------------

    async def status(self) -> SourceStatus:
        if self.root is None:
            return SourceStatus(
                name=self.name,
                connected=False,
                detail="ATLAS_MAIL_FOLDER is not set.",
                hint="Set ATLAS_MAIL_FOLDER in .env to the folder your Power Automate flow writes to "
                "(docs/INBOX_SETUP.md).",
            )
        if not self.root.is_dir():
            return SourceStatus(
                name=self.name,
                connected=False,
                account=str(self.root),
                detail=f"Folder not found: {self.root}",
                hint="Check ATLAS_MAIL_FOLDER, and that OneDrive has synced the folder to this computer.",
            )
        count = sum(1 for _ in self._files())
        hint = None
        if not self.me:
            hint = "Set ATLAS_MAIL_ME to your email address(es) so ATLAS can tell which messages you sent."
        return SourceStatus(
            name=self.name,
            connected=True,
            account=str(self.root),
            detail=f"Watching {self.root} ({count} mail file{'s' if count != 1 else ''}). Read-only.",
            hint=hint,
            can_draft=False,
        )

    async def list_messages(self, since: datetime, limit: int = 200) -> list[MailMessage]:
        return await asyncio.to_thread(self._list_sync, _aware(since), limit)

    async def get_body(self, message_id: str) -> str:
        return await asyncio.to_thread(self._body_sync, message_id)

    async def create_outlook_draft(self, draft: EmailDraft) -> str | None:
        return None  # the folder source cannot write to Outlook; the engine exports an .eml instead

    # -- internals --------------------------------------------------------------------------------------------

    def _files(self):
        if self.root is None or not self.root.is_dir():
            return
        for path in self.root.rglob("*"):
            if path.suffix.lower() in SUFFIXES and path.is_file() and not path.name.startswith((".", "~$")):
                yield path

    def _list_sync(self, since: datetime, limit: int) -> list[MailMessage]:
        since_ts = since.timestamp()
        found: dict[str, MailMessage] = {}
        for path in self._files():
            try:
                st = path.stat()
                if st.st_mtime < since_ts or st.st_size > MAX_FILE_BYTES:
                    continue  # a file can't have been written before the mail arrived
                parsed = self._parse(path)
            except Exception as exc:  # noqa: BLE001 — one bad file must not stop the scan
                log.warning("Skipping unreadable mail file %s: %s", path.name, exc)
                continue
            if parsed is None or parsed.msg.received_at < since:
                continue
            self._paths[parsed.msg.id] = path
            found[parsed.msg.id] = parsed.msg
        return sorted(found.values(), key=lambda m: m.received_at, reverse=True)[:limit]

    def _body_sync(self, message_id: str) -> str:
        path = self._paths.get(message_id)
        if path is None or not path.exists():
            for candidate in self._files():  # not listed in this process yet: find it
                try:
                    parsed = self._parse(candidate)
                except Exception:  # noqa: BLE001, S112 — unreadable files were already logged by the scan
                    continue
                if parsed and parsed.msg.id == message_id:
                    self._paths[message_id] = candidate
                    return trim_quoted(parsed.body)
            raise KeyError(f"mail file for {message_id} not found")
        parsed = self._parse(path)
        return trim_quoted(parsed.body) if parsed else ""

    def _parse(self, path: Path) -> _Parsed | None:
        suffix = path.suffix.lower()
        if suffix == ".json":
            return self._parse_json(path)
        if suffix == ".eml":
            return self._parse_eml(path)
        if suffix == ".msg":
            return self._parse_msg(path)
        return None

    def _is_from_me(self, sender: str, path: Path) -> bool:
        return _in_sent_dir(path, self.root) or (address_of(sender) in self.me if self.me else False)

    def _build(self, path: Path, *, id_: str | None, imid, conv, subject, sender, to, cc, when, link, body):
        received = when or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        msg = MailMessage(
            id=str(id_) if id_ else _file_id(path, self.root),
            internet_message_id=(str(imid).strip() or None) if imid else None,
            conversation_id=str(conv) if conv else None,
            subject=(subject or "").strip() or "(no subject)",
            sender=sender,
            to=to,
            cc=cc,
            received_at=received,
            is_from_me=self._is_from_me(sender, path),
            web_link=link or None,
            preview=make_preview(trim_quoted(body, max_chars=1000)),
        )
        return _Parsed(msg, body)

    def _parse_json(self, path: Path) -> _Parsed | None:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and isinstance(data.get("body"), dict) and "subject" not in data:
            data = data["body"]  # a whole trigger output ({"headers":..., "body": {...}})
        if not isinstance(data, dict):
            return None
        body = _ci(data, "Body", "body") or _ci(data, "BodyPreview", "bodyPreview") or ""
        if isinstance(body, dict):  # Graph shape {contentType, content}
            body = body.get("content") or ""
        body = str(body)
        is_html = _ci(data, "IsHtml", "isHtml")
        if is_html is True or (is_html is None and looks_like_html(body)):
            body = html_to_text(body)
        sender_raw = _ci(data, "From", "from", "Sender", "sender")
        sender_list = _addr_list(sender_raw)
        return self._build(
            path,
            id_=_ci(data, "Id", "id", "MessageId"),
            imid=_ci(data, "InternetMessageId", "internetMessageId"),
            conv=_ci(data, "ConversationId", "conversationId"),
            subject=_ci(data, "Subject", "subject"),
            sender=sender_list[0] if sender_list else "",
            to=_addr_list(_ci(data, "To", "toRecipients", "to")),
            cc=_addr_list(_ci(data, "Cc", "ccRecipients", "cc")),
            when=_parse_dt(_ci(data, "DateTimeReceived", "receivedDateTime", "sentDateTime", "Date")),
            link=_ci(data, "WebLink", "webLink"),
            body=body,
        )

    def _parse_eml(self, path: Path) -> _Parsed:
        with path.open("rb") as fh:
            m = email.message_from_binary_file(fh, policy=email.policy.default)
        body = ""
        part = m.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = html_to_text(content) if part.get_content_type() == "text/html" else content
        senders = _addr_list(str(m.get("From", "")))
        return self._build(
            path,
            id_=None,
            imid=m.get("Message-ID"),
            conv=m.get("Thread-Index") or None,
            subject=str(m.get("Subject", "")),
            sender=senders[0] if senders else "",
            to=_addr_list(str(m.get("To", ""))),
            cc=_addr_list(str(m.get("Cc", ""))),
            when=_parse_dt(str(m.get("Date", ""))),
            link=None,
            body=body,
        )

    def _parse_msg(self, path: Path) -> _Parsed:
        m = _open_msg(path)
        try:
            body = m.body or ""
            if not body.strip() and getattr(m, "htmlBody", None):
                html = m.htmlBody
                body = html_to_text(html.decode("utf-8", "replace") if isinstance(html, bytes) else html)
            senders = _addr_list(m.sender or "")
            return self._build(
                path,
                id_=None,
                imid=getattr(m, "messageId", None),
                conv=None,
                subject=m.subject or "",
                sender=senders[0] if senders else "",
                to=_addr_list(m.to or ""),
                cc=_addr_list(m.cc or ""),
                when=_parse_dt(m.date),
                link=None,
                body=body,
            )
        finally:
            close = getattr(m, "close", None)
            if close:
                close()
