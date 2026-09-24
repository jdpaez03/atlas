"""Mission history: an append-only SQLite log of every published event (docs/PHASE3.md, section B).

    events(seq INTEGER PRIMARY KEY, type TEXT, mission_id TEXT, ts TEXT, json TEXT)

`WorldState` is `fold(events)`, so the log is the whole history: on startup the events are replayed into
the `WorldStore` and seed the `EventBus` history (WS replay keeps working across restarts). Writes are
synchronous and tiny (WAL, synchronous=NORMAL): one INSERT per event, in publish order.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from .models import AtlasEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    mission_id TEXT,
    ts         TEXT NOT NULL,
    json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_mission ON events (mission_id, seq);
"""


class EventLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def append(self, event: AtlasEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events (seq, type, mission_id, ts, json) VALUES (?, ?, ?, ?, ?)",
                (event.seq, str(getattr(event.type, "value", event.type)), event.mission_id,
                 event.ts.isoformat(), event.model_dump_json()),
            )

    def events(self, since_seq: int = 0, mission_id: str | None = None) -> Iterator[AtlasEvent]:
        sql = "SELECT json FROM events WHERE seq > ?"
        args: list[object] = [since_seq]
        if mission_id is not None:
            sql += " AND mission_id = ?"
            args.append(mission_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY seq", args).fetchall()
        for (raw,) in rows:
            yield AtlasEvent.model_validate_json(raw)

    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def first_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MIN(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def clear(self) -> None:
        """Delete every event (POST /reset). Sequence numbers keep growing: the bus owns them."""
        with self._lock:
            self._conn.execute("DELETE FROM events")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
