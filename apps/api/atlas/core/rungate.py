"""One background run at a time: inbox scans, CC digests, ARGOS watches and briefs share this gate.

The inbox engine and the ARGOS engine hold the same `RunGate` (created by the inbox engine, handed to ARGOS in
main.py). A run claims it with `hold(label, mission_id, task)` while `lock` is held; `busy` is true until
that task finishes. Starting anything while `busy` is refused (409 in the API).
"""

from __future__ import annotations

import asyncio


class RunGate:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()  # serializes the check-then-start of every run
        self._task: asyncio.Task[None] | None = None
        self.label: str | None = None
        self.mission_id: str | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def hold(self, label: str, mission_id: str | None, task: asyncio.Task[None]) -> None:
        self._task, self.label, self.mission_id = task, label, mission_id

    def describe(self) -> str:
        """'An inbox scan is running' — for 409 messages."""
        return f"{self.label or 'Another run'} is running" if self.busy else "Nothing is running"
