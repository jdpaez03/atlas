"""In-process event bus. Every state change in ATLAS is published here and
fanned out to WebSocket subscribers (the Command Center) and the history log.
"""

from __future__ import annotations

import asyncio
from collections import deque

from .models import AtlasEvent


class EventBus:
    def __init__(self, history_size: int = 2000):
        self._subscribers: set[asyncio.Queue[AtlasEvent]] = set()
        self._history: deque[AtlasEvent] = deque(maxlen=history_size)
        self._seq = 0

    async def publish(self, event: AtlasEvent) -> AtlasEvent:
        self._seq += 1
        event.seq = self._seq
        self._history.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

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
        return [
            e
            for e in self._history
            if e.seq > since_seq and (mission_id is None or e.mission_id == mission_id)
        ]
