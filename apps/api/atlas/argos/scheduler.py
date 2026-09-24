"""ARGOS scheduler (docs/ARGOS.md): watches at local times on weekdays, the L10 brief once a week.

    ATLAS_ARGOS_SCHEDULE   comma-separated local times (America/Mexico_City), weekdays; default "09:30,16:00".
                           Empty disables scheduled watches (POST /argos/run still works).
    ATLAS_ARGOS_BRIEF      "<DAY> HH:MM" (MON..SUN, comma-separated for several); default "MON 07:30".
                           Empty disables the scheduled brief (POST /argos/brief still works).

At startup a missed slot runs once (catch-up), like the inbox scheduler: a watch if the last full watch is older
than the most recent watch slot, a brief if the last brief is older than the most recent brief slot. Each slot
fires at most once; when another run holds the shared gate (an inbox scan, say) the slot is retried every
`retry` seconds until it can start. The loop waits for a run to finish before the next one (so a brief due at
the same time follows the watch). `stop()` cancels the loop immediately (a running watch is the engine's to stop).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

from ..inbox.state import aware
from .engine import ArgosBusyError, ArgosEngine, ArgosError

log = logging.getLogger("atlas.argos")

DEFAULT_SCHEDULE = "09:30,16:00"
DEFAULT_BRIEF = "MON 07:30"
POLL_SECONDS = 60.0
RETRY_SECONDS = 120.0
STARTUP_DELAY = 5.0  # let the inbox scheduler's catch-up scan claim the shared gate first (it drops busy slots)
DAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
        "LUN": 0, "MAR": 1, "MIE": 2, "MIÉ": 2, "JUE": 3, "VIE": 4, "SAB": 5, "SÁB": 5, "DOM": 6}
DAY_LABELS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _time(raw: str) -> time:
    hh, _, mm = raw.strip().partition(":")
    return time(int(hh), int(mm or 0))


def parse_schedule(raw: str | None) -> list[time]:
    """'09:30, 16:00' -> [09:30, 16:00]. None -> the default; '' -> [] (disabled)."""
    if raw is None:
        raw = DEFAULT_SCHEDULE
    out: set[time] = set()
    for part in raw.replace(";", ",").split(","):
        if not part.strip():
            continue
        try:
            out.add(_time(part))
        except ValueError:
            log.warning("ATLAS_ARGOS_SCHEDULE: ignoring %r (use HH:MM)", part.strip())
    return sorted(out)


def parse_brief(raw: str | None) -> list[tuple[int, time]]:
    """'MON 07:30' -> [(0, 07:30)]. None -> the default; '' -> [] (disabled)."""
    if raw is None:
        raw = DEFAULT_BRIEF
    out: set[tuple[int, time]] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        day, _, hhmm = part.partition(" ")
        try:
            out.add((DAYS[day.strip().upper()[:3]], _time(hhmm or "07:30")))
        except (KeyError, ValueError):
            log.warning("ATLAS_ARGOS_BRIEF: ignoring %r (use e.g. 'MON 07:30')", part)
    return sorted(out)


class ArgosScheduler:
    def __init__(
        self,
        engine: ArgosEngine,
        times: list[time] | None = None,
        brief: list[tuple[int, time]] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        weekdays_only: bool = True,
        poll: float = POLL_SECONDS,
        retry: float = RETRY_SECONDS,
        startup_delay: float = STARTUP_DELAY,
    ):
        self.engine = engine
        self.times = sorted(times) if times is not None else parse_schedule(os.getenv("ATLAS_ARGOS_SCHEDULE"))
        self.brief = sorted(brief) if brief is not None else parse_brief(os.getenv("ATLAS_ARGOS_BRIEF"))
        self.clock = clock or engine.now
        self._sleep = sleep or asyncio.sleep
        self.weekdays_only = weekdays_only
        self.poll = poll
        self.retry = retry
        self.startup_delay = startup_delay
        self.tz = engine.tz
        self._task: asyncio.Task[None] | None = None
        self._attempted: dict[str, datetime] = {}  # kind -> the slot last fired (never twice)
        self._quiet: set[str] = set()  # kinds whose "cannot start" was already logged
        self.fired: list[tuple[datetime, str, str]] = []  # (when, kind, reason) — status/tests
        engine.scheduler = self

    # -- slot arithmetic (pure) ---------------------------------------------

    def _watch_slots(self, day: datetime) -> list[datetime]:
        if self.weekdays_only and day.weekday() >= 5:
            return []
        return [datetime.combine(day.date(), t, tzinfo=self.tz) for t in self.times]

    def _brief_slots(self, day: datetime) -> list[datetime]:
        return [datetime.combine(day.date(), t, tzinfo=self.tz) for d, t in self.brief if d == day.weekday()]

    def _next(self, slots: Callable[[datetime], list[datetime]], after: datetime) -> datetime | None:
        local = aware(after).astimezone(self.tz)
        for i in range(9):
            for slot in slots(local + timedelta(days=i)):
                if slot > local:
                    return slot
        return None

    def _previous(self, slots: Callable[[datetime], list[datetime]], at: datetime) -> datetime | None:
        local = aware(at).astimezone(self.tz)
        for i in range(9):
            for slot in reversed(slots(local - timedelta(days=i))):
                if slot <= local:
                    return slot
        return None

    def next_slot(self, after: datetime) -> datetime | None:
        return self._next(self._watch_slots, after) if self.times else None

    def previous_slot(self, at: datetime) -> datetime | None:
        return self._previous(self._watch_slots, at) if self.times else None

    def next_brief_slot(self, after: datetime) -> datetime | None:
        return self._next(self._brief_slots, after) if self.brief else None

    def previous_brief_slot(self, at: datetime) -> datetime | None:
        return self._previous(self._brief_slots, at) if self.brief else None

    def due(self, now: datetime) -> list[tuple[str, datetime]]:
        """[(kind, slot)] that should run now: 'watch' first, then 'brief'."""
        out = []
        for kind, prev, last in (("watch", self.previous_slot(now), self.engine.last_watch),
                                 ("brief", self.previous_brief_slot(now), self.engine.last_brief)):
            if prev is None or self._attempted.get(kind) == prev:
                continue
            if last is None or aware(last) < prev:
                out.append((kind, prev))
        return out

    # -- status --------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def next_run(self) -> datetime | None:
        return self.next_slot(self.clock()) if self.running else None

    def next_brief(self) -> datetime | None:
        return self.next_brief_slot(self.clock()) if self.running else None

    def schedule_labels(self) -> list[str]:
        return [t.strftime("%H:%M") for t in self.times]

    def brief_labels(self) -> list[str]:
        return [f"{DAY_LABELS[d]} {t.strftime('%H:%M')}" for d, t in self.brief]

    # -- loop ----------------------------------------------------------------

    async def start(self) -> None:
        if not self.times and not self.brief:
            log.info("ARGOS scheduler disabled (ATLAS_ARGOS_SCHEDULE and ATLAS_ARGOS_BRIEF are empty)")
            return
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="argos-scheduler")

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
            log.exception("ARGOS scheduler raised while stopping")

    async def _loop(self) -> None:
        first = True
        try:
            await self._idle()
            if self.startup_delay > 0 and self.due(self.clock()):
                await asyncio.sleep(self.startup_delay)
            while True:
                busy = False
                for kind, slot in self.due(self.clock()):
                    outcome = await self._fire(kind, slot, "catch-up" if first else "scheduled")
                    busy = busy or outcome == "busy"
                first = False
                now = aware(self.clock())
                targets = [t for t in (self.next_slot(now), self.next_brief_slot(now)) if t is not None]
                if busy:
                    targets.append(now + timedelta(seconds=self.retry))
                if not targets:  # pragma: no cover - unreachable with a schedule
                    return
                target = min(targets)
                await self._idle()
                while True:
                    remaining = (target - aware(self.clock())).total_seconds()
                    if remaining <= 0:
                        break
                    await self._sleep(min(self.poll, remaining))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never crash the server
            log.exception("ARGOS scheduler stopped")

    async def _idle(self) -> None:
        try:
            await self.engine.set_idle()
        except Exception:
            log.debug("ARGOS idle state not set", exc_info=True)

    async def _fire(self, kind: str, slot: datetime, reason: str) -> str:
        """'started' | 'busy' | 'skipped'. A started run is awaited before returning."""
        try:
            if kind == "watch":
                await self.engine.start_run(trigger=reason)
            else:
                await self.engine.start_brief(trigger=reason)
        except ArgosBusyError:
            log.info("ARGOS %s %s postponed: %s", reason, kind, self.engine.gate.describe())
            return "busy"
        except ArgosError as exc:
            self._attempted[kind] = slot
            if kind not in self._quiet:
                self._quiet.add(kind)
                log.info("ARGOS %s %s skipped: %s%s", reason, kind, exc, f" · {exc.hint}" if exc.hint else "")
            return "skipped"
        except Exception:
            self._attempted[kind] = slot
            log.exception("ARGOS %s %s could not start", reason, kind)
            return "skipped"
        self._attempted[kind] = slot
        self._quiet.discard(kind)
        self.fired.append((aware(self.clock()), kind, reason))
        await self.engine.wait()
        return "started"
