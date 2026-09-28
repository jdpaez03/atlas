"""Teams transcripts check (atlas/argos/transcripts.py) against a fake Microsoft Graph."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest

from atlas.argos import transcripts as T
from atlas.argos.checks import CheckContext, CheckNotConfigured
from atlas.core.events import EventBus
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore
from atlas.live.files import FileSandbox, FileTools

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)
JOIN = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=x"

VTT = """WEBVTT

1c0a7b2e-1111-2222-3333-444455556666/12-0
00:00:01.200 --> 00:00:04.000
<v Juan Diego Paez>Buenos días, arrancamos con los Rocks.</v>

1c0a7b2e-1111-2222-3333-444455556666/13-0
00:00:04.100 --> 00:00:07.000
<v Juan Diego Paez>B600 va atrasado en escrituración.</v>

00:01:05.000 --> 00:01:09.500
<v Josué Pérez>Balcones mandará el dashboard hoy.</v>
"""


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path).removeprefix("/v1.0")
        self.calls.append(path)
        assert request.headers["authorization"] == "Bearer tok"
        if path == "/me/calendarView":
            ev = lambda subj, s, e, join=JOIN: {
                "subject": subj, "isCancelled": False, "onlineMeeting": {"joinUrl": join},
                "start": {"dateTime": s, "timeZone": "UTC"}, "end": {"dateTime": e, "timeZone": "UTC"}}
            return httpx.Response(200, json={"value": [
                ev("Junta Semanal", "2026-09-21T15:00:00.0000000", "2026-09-21T16:00:00.0000000"),
                ev("JUNTA SEMANAL", "2026-09-28T13:00:00.0000000", "2026-09-28T14:00:00.0000000"),
                ev("Comité de inversión", "2026-09-22T15:00:00.0000000", "2026-09-22T16:00:00.0000000",
                   "https://teams.microsoft.com/other"),
            ]})
        if path == "/me/onlineMeetings":
            assert request.url.params["$filter"] == f"JoinWebUrl eq '{JOIN}'"
            return httpx.Response(200, json={"value": [{"id": "MEET1"}]})
        if path == "/me/onlineMeetings/MEET1/transcripts":
            return httpx.Response(200, json={"value": [
                {"id": "TR1", "createdDateTime": "2026-09-21T16:02:11.7411768Z"},
                {"id": "OLD", "createdDateTime": "2026-07-01T16:00:00Z"}]})
        if path == "/me/onlineMeetings/MEET1/transcripts/TR1/content":
            assert request.headers["accept"] == "text/vtt"
            return httpx.Response(200, text=VTT, headers={"content-type": "text/vtt"})
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})


def ctx() -> CheckContext:
    return CheckContext(store=WorldStore(AgentRegistry.load(), EventBus()), scope=None, mail=None, config={}, now=NOW)


def test_vtt_to_text_merges_turns_and_drops_cue_ids():
    text = T.vtt_to_text(VTT)
    assert text.splitlines()[0] == ("[00:01] Juan Diego Paez: Buenos días, arrancamos con los Rocks. B600 va "
                                    "atrasado en escrituración.")
    assert "[01:05] Josué Pérez: Balcones mandará el dashboard hoy." in text
    assert "1c0a7b2e" not in text and "-->" not in text


def test_not_configured_without_meetings(monkeypatch):
    monkeypatch.delenv("ATLAS_TRANSCRIPT_MEETINGS", raising=False)
    with pytest.raises(CheckNotConfigured):
        T.TranscriptsCheck(token=lambda: "tok").preflight({})


def test_saves_new_transcripts_once_and_flags_a_missing_one(monkeypatch):
    monkeypatch.setenv("ATLAS_TRANSCRIPT_MEETINGS", "junta semanal")
    fake = FakeGraph()
    check = T.TranscriptsCheck(token=lambda: "tok", transport=httpx.MockTransport(fake.handler))
    res = asyncio.run(check.run(ctx()))
    files = sorted(T.transcripts_dir("corporate").glob("*.txt"))
    assert [f.name for f in files] == ["2026-09-21 Junta Semanal.txt"]
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("# Junta Semanal\nFecha: 2026-09-21") and "B600 va atrasado" in body
    assert "Saved 1 new transcript(s)" in res.notes[0]
    # the 28-Sep occurrence ended 5 h ago with no transcript: one LOW alert; the old transcript is out of range
    assert [(a.severity, a.title) for a in res.alerts] == [("LOW", "Junta Semanal 2026-09-28: sin transcripción")]
    assert fake.calls.count("/me/onlineMeetings") == 1  # one online meeting for the recurring series
    assert not any("OLD" in c for c in fake.calls)

    again = asyncio.run(check.run(ctx()))
    assert again.notes == ["No new transcripts"] and len(list(T.transcripts_dir("corporate").glob("*.txt"))) == 1

    # the agents of the node can read the folder
    tools = FileTools(FileSandbox.for_mission("corporate", "msn_t1"))
    found = tools.run("search_files", {"query": "junta semanal"})
    assert "2026-09-21 Junta Semanal.txt" in found.text
    read = tools.run("read_file", {"path": str(files[0])})
    assert read.ok and "Balcones mandará el dashboard hoy" in read.text
    other = FileTools(FileSandbox.for_mission("personal", "msn_t2")).run("read_file", {"path": str(files[0])})
    assert not other.ok


def test_refused_scope_is_a_setup_step(monkeypatch):
    monkeypatch.setenv("ATLAS_TRANSCRIPT_MEETINGS", "junta semanal")
    transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"code": "Forbidden",
                                                                                   "message": "no consent"}}))
    with pytest.raises(CheckNotConfigured) as exc:
        asyncio.run(T.TranscriptsCheck(token=lambda: "tok", transport=transport).run(ctx()))
    assert "HTTP 403" in str(exc.value) and "atlas-graph login" in exc.value.hint
