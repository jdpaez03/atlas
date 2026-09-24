"""Inbox scheduler (docs/INBOX.md §2): scans at local times on weekdays while ATLAS runs.

    ATLAS_INBOX_SCHEDULE   comma-separated local times (America/Mexico_City), default "08:00,15:00";
                           set it empty to disable scheduled scans (POST /inbox/scan still works)

At startup, one catch-up scan runs if the last scan is older than the most recent slot. With no mail source
(or one that is not connected) the scheduler logs once and idles; it picks the source up at the next slot.
Scans never overlap (the engine refuses a second one). `stop()` cancels the loop immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

from .engine import InboxEngine, InboxError, ScanBusyError
from .state import aware

log = logging.getLogger("atlas.inbox")

DEFAULT_SCHEDULE = "08:00,15:00"
POLL_SECONDS = 60.0  # re-read the clock at least this often (sleep, clock changes, suspend/resume)


def parse_schedule(raw: str | None) -> list[time]:
    """'08:00, 15:30' -> [08:00, 15:30] (sorted, unique). None -> the default; '' -> [] (disabled)."""
    if raw is None:
        raw = DEFAULT_SCHEDULE
    out: set[time] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, _, mm = part.partition(":")
            out.add(time(int(hh), int(mm or 0)))
        except ValueError:
            log.warning("ATLAS_INBOX_SCHEDULE: ignoring %r (use HH:MM)", part)
    return sorted(out)


class InboxScheduler:
    def __init__(
        self,
        engine: InboxEngine,
        times: list[time] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        weekdays_only: bool = True,
        poll: float = POLL_SECONDS,
    ):
        self.engine = engine
        self.times = sorted(times) if times is not None else parse_schedule(os.getenv("ATLAS_INBOX_SCHEDULE"))
        self.clock = clock or engine.now
        self._sleep = sleep or asyncio.sleep
        self.weekdays_only = weekdays_only
        self.poll = poll
        self.tz = engine.tz
        self._task: asyncio.Task[None] | None = None
        self._idle_logged = False
        self.fired: list[tuple[datetime, str]] = []  # (when, reason) — for status/tests
        engine.scheduler = self

    # -- slot arithmetic (pure) ---------------------------------------------

    def _day_ok(self, d: datetime) -> bool:
        return not self.weekdays_only or d.weekday() < 5

    def _slots_on(self, day: datetime) -> list[datetime]:
        return [datetime.combine(day.date(), t, tzinfo=self.tz) for t in self.times] if self._day_ok(day) else []

    def next_slot(self, after: datetime) -> datetime | None:
        """The first slot strictly after `after`."""
        if not self.times:
            return None
        local = aware(after).astimezone(self.tz)
        for i in range(9):
            for slot in self._slots_on(local + timedelta(days=i)):
                if slot > local:
                    return slot
        return None

    def previous_slot(self, at: datetime) -> datetime | None:
        """The last slot at or before `at`."""
        if not self.times:
            return None
        local = aware(at).astimezone(self.tz)
        for i in range(9):
            for slot in reversed(self._slots_on(local - timedelta(days=i))):
                if slot <= local:
                    return slot
        return None

    def missed(self, now: datetime) -> bool:
        prev = self.previous_slot(now)
        last = self.engine.last_scan
        return prev is not None and (last is None or aware(last) < prev)

    # -- status --------------------------------------------------------------

    def next_scan(self) -> datetime | None:
        return self.next_slot(self.clock()) if self.running else None

    def schedule_labels(self) -> list[str]:
        return [t.strftime("%H:%M") for t in self.times]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- loop ----------------------------------------------------------------

    async def start(self) -> None:
        if not self.times:
            log.info("inbox scheduler disabled (ATLAS_INBOX_SCHEDULE is empty)")
            return
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="inbox-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2)
        except (asyncio.CancelledError, TimeoutError):
            pass
        except Exception:  # pragma: no cover
            log.exception("inbox scheduler raised while stopping")

    async def _loop(self) -> None:
        try:
            if self.engine.source is None:
                self._idle_logged = True
                log.info("inbox scheduler idle: no mail source configured (scans at %s on weekdays once one is)",
                         ", ".join(self.schedule_labels()))
            if self.missed(self.clock()):
                await self._fire("catch-up")
            while True:
                slot = self.next_slot(self.clock())
                if slot is None:  # unreachable with at least one time
                    return
                while True:
                    remaining = (slot - aware(self.clock())).total_seconds()
                    if remaining <= 0:
                        break
                    await self._sleep(min(self.poll, remaining))
                await self._fire("scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never crash the server
            log.exception("inbox scheduler stopped")

    async def _fire(self, reason: str) -> bool:
        """Start a scan if possible. Returns True when one started."""
        try:
            await self.engine.start_scan(trigger=reason)
        except ScanBusyError:
            log.info("inbox %s scan skipped: a scan is already running", reason)
            return False
        except InboxError as exc:
            if not self._idle_logged:
                self._idle_logged = True
                log.info("inbox scheduler idle (%s scan skipped): %s%s", reason, exc,
                         f" · {exc.hint}" if exc.hint else "")
            else:
                log.debug("inbox %s scan skipped: %s", reason, exc)
            return False
        except Exception:
            log.exception("inbox %s scan could not start", reason)
            return False
        self._idle_logged = False
        self.fired.append((aware(self.clock()), reason))
        return True
