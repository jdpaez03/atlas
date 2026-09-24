"""In-process event bus. Every state change in ATLAS is published here and
fanned out to WebSocket subscribers (the Command Center) and the history log.

With an `EventLog` (core/db.py) every published event is also appended to SQLite, and `seed()` restores
the sequence number and the in-memory history after a restart. `history()` falls back to the log for
events older than the in-memory window, so WS replay works for any `since`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .models import AtlasEvent

if TYPE_CHECKING:
    from .db import EventLog

log = logging.getLogger("atlas.events")


class EventBus:
    def __init__(self, history_size: int = 2000, *, log: EventLog | None = None):
        self._subscribers: set[asyncio.Queue[AtlasEvent]] = set()
        self._history: deque[AtlasEvent] = deque(maxlen=history_size)
        self._seq = 0
        self.log = log

    async def publish(self, event: AtlasEvent) -> AtlasEvent:
        self._seq += 1
        event.seq = self._seq
        self._history.append(event)
        if self.log is not None:
            try:
                self.log.append(event)
            except Exception:  # never let a disk problem stop the engine
                log.exception("could not persist event %s", event.seq)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def seed(self, events: Iterable[AtlasEvent]) -> None:
        """Restore history and sequence from persisted events (startup, before anything is published)."""
        for e in events:
            self._history.append(e)
            self._seq = max(self._seq, e.seq)
        if self.log is not None:
            self._seq = max(self._seq, self.log.last_seq())

    @property
    def last_seq(self) -> int:
        """Sequence number of the most recently published event (0 if none)."""
        return self._seq

    def subscribe(self) -> asyncio.Queue[AtlasEvent]:
        queue: asyncio.Queue[AtlasEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[AtlasEvent]) -> None:
        self._subscribers.discard(queue)

    def history(self, since_seq: int = 0, mission_id: str | None = None) -> list[AtlasEvent]:
        oldest = self._history[0].seq if self._history else self._seq + 1
        if self.log is not None and since_seq + 1 < oldest:
            try:
                return list(self.log.events(since_seq, mission_id))
            except Exception:  # pragma: no cover - fall back to what is in memory
                log.exception("could not read the event log")
        return [
            e
            for e in self._history
            if e.seq > since_seq and (mission_id is None or e.mission_id == mission_id)
        ]
