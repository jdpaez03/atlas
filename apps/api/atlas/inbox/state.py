"""Inbox scan state: `<ATLAS_LOCAL_DIR>/inbox/state.json` (docs/INBOX.md §2).

    {
      "version": 1,
      "last_scan": "2026-09-24T14:00:00+00:00" | null,     start of the last completed scan
      "processed": {message_id: {"at": received iso, "conv": conversation id?, "imid": internet id?, "out": bool}},
      "conversations": {conversation_id: {"last_in": iso?, "last_out": iso?}},
      "retry": {message_id: {"at": received iso, "tries": n}}   unreadable / not analyzed: retried next scans
    }

Privacy: ids and timestamps only. No subject, sender, preview or body is ever written here.
Also: the user's timezone (America/Mexico_City) and business-day arithmetic.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from ..core import paths

log = logging.getLogger("atlas.inbox")

TZ_NAME = "America/Mexico_City"
KEEP_DAYS = 120  # processed ids older than this are pruned (a scan never looks back that far)
MAX_TRIES = 3  # a message that fails this many scans is given up (marked processed, reported)


def user_tz() -> tzinfo:
    """America/Mexico_City. Windows has no system tz database (tzdata may be missing): Mexico dropped DST in
    2022, so a fixed UTC-06:00 is exact for current dates."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, missing tzdata
        return timezone(timedelta(hours=-6), "CST")


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def business_days_between(start: date, end: date) -> int:
    """Weekdays strictly after `start`, up to and including `end` (Mon sent, Thu today -> 3)."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def state_path() -> Path:
    return paths.local_dir() / "inbox" / "state.json"


def _iso(dt: datetime | None) -> str | None:
    return aware(dt).astimezone(UTC).isoformat() if dt else None


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return aware(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None


@dataclass
class InboxState:
    path: Path
    last_scan: datetime | None = None
    processed: dict[str, dict[str, Any]] = field(default_factory=dict)
    conversations: dict[str, dict[str, str]] = field(default_factory=dict)
    retry: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> InboxState:
        path = path or state_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError):
            log.warning("inbox state %s is unreadable; starting fresh", path)
            return cls(path)
        return cls(
            path,
            last_scan=_parse(raw.get("last_scan")),
            processed={str(k): dict(v) for k, v in (raw.get("processed") or {}).items() if isinstance(v, dict)},
            conversations={str(k): dict(v) for k, v in (raw.get("conversations") or {}).items()
                           if isinstance(v, dict)},
            retry={str(k): dict(v) for k, v in (raw.get("retry") or {}).items() if isinstance(v, dict)},
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "last_scan": _iso(self.last_scan), "processed": self.processed,
                "conversations": self.conversations, "retry": self.retry}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- messages ------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed

    def mark_processed(self, msg: Any) -> None:
        self.retry.pop(msg.id, None)
        self.processed[msg.id] = {
            "at": _iso(msg.received_at), "conv": msg.conversation_id, "imid": msg.internet_message_id,
            "out": bool(msg.is_from_me),
        }

    def mark_failed(self, msg: Any) -> bool:
        """Remember a message to retry. Returns True when it has now failed MAX_TRIES times (given up: it is
        marked processed)."""
        entry = self.retry.setdefault(msg.id, {"at": _iso(msg.received_at), "tries": 0})
        entry["tries"] = int(entry.get("tries", 0)) + 1
        if entry["tries"] >= MAX_TRIES:
            self.retry.pop(msg.id, None)
            self.mark_processed(msg)
            return True
        return False

    def oldest_retry(self) -> datetime | None:
        return min((d for d in (_parse(v.get("at")) for v in self.retry.values()) if d), default=None)

    def observe(self, msg: Any) -> None:
        """Remember when each conversation last had an inbound / outbound message (for staleness)."""
        if not msg.conversation_id:
            return
        conv = self.conversations.setdefault(msg.conversation_id, {})
        key = "last_out" if msg.is_from_me else "last_in"
        when = aware(msg.received_at)
        current = _parse(conv.get(key))
        if current is None or when > current:
            conv[key] = _iso(when) or ""

    def conversation_of(self, message_id: str | None) -> str | None:
        if not message_id:
            return None
        return (self.processed.get(message_id) or {}).get("conv")

    def internet_id_of(self, message_id: str | None) -> str | None:
        if not message_id:
            return None
        return (self.processed.get(message_id) or {}).get("imid")

    def reply_after(self, conversation_id: str | None, when: datetime | None) -> bool:
        """Did the other side write in this conversation after `when`?"""
        if not conversation_id or when is None:
            return False
        last_in = _parse((self.conversations.get(conversation_id) or {}).get("last_in"))
        return last_in is not None and last_in > aware(when)

    def prune(self, now: datetime) -> None:
        cutoff = aware(now) - timedelta(days=KEEP_DAYS)
        self.processed = {k: v for k, v in self.processed.items()
                          if (_parse(v.get("at")) or aware(now)) >= cutoff}
        keep: dict[str, dict[str, str]] = {}
        for k, v in self.conversations.items():
            last = max((d for d in (_parse(v.get("last_in")), _parse(v.get("last_out"))) if d), default=None)
            if last is None or last >= cutoff:
                keep[k] = v
        self.conversations = keep
