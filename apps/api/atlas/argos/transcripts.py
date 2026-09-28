"""Teams transcripts check (docs/ARGOS.md § Teams transcripts): the text of your recurring meetings, for the agents.

    ATLAS_TRANSCRIPT_MEETINGS   subjects to follow, ';'-separated, matched without case or accents
                                (e.g. "junta semanal"). Empty = the check is off.
    ATLAS_TRANSCRIPT_LOOKBACK_DAYS   how far back to look (default 30)

Each run (with the other ARGOS checks, or on demand in Monitor):

  1. your calendar (/me/calendarView) → the Teams meetings whose subject matches, within the lookback;
  2. each meeting's online meeting (/me/onlineMeetings?$filter=JoinWebUrl eq …) → its transcripts (a recurring
     series shares one online meeting, so every occurrence's transcript is listed there);
  3. every transcript not saved yet → its WebVTT content → plain text "[mm:ss] Speaker: text", saved as
     <ATLAS_LOCAL_DIR>/transcripts/<node>/<YYYY-MM-DD> <Subject>.txt. Agents of the node read that folder with
     their file tools (it is one of their read roots), so a mission can ask for "the latest junta semanal".

A matched meeting that ended more than 3 hours ago and has no transcript raises a LOW alert (transcription was
probably not turned on). Read-only on Microsoft 365. Delegated scopes: Calendars.Read, OnlineMeetings.Read and
OnlineMeetingTranscript.Read.All (admin consent); sign in again with `atlas-graph login` after adding them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..core import paths
from .checks import AlertDraft, CheckContext, CheckNotConfigured, CheckResult, fingerprint
from .l10 import record_evidence

log = logging.getLogger("atlas.argos.transcripts")

NAME = "transcripts"
GRAPH = "https://graph.microsoft.com/v1.0"
TRANSCRIPT_SCOPES = ["Calendars.Read", "OnlineMeetings.Read", "OnlineMeetingTranscript.Read.All"]
DEFAULT_LOOKBACK_DAYS = 30
MISSING_AFTER = timedelta(hours=3)
MISSING_WINDOW = timedelta(days=8)  # only recent occurrences can raise "no transcript"
HINT = ("Set ATLAS_TRANSCRIPT_MEETINGS (e.g. \"junta semanal\") and ATLAS_MS_CLIENT_ID in .env; the Entra app "
        "needs Calendars.Read, OnlineMeetings.Read and OnlineMeetingTranscript.Read.All (admin consent); then "
        "run `uv run atlas-graph login` and restart the API")


def fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", str(text or ""))
    return " ".join("".join(c for c in norm if not unicodedata.combining(c)).lower().split())


def meeting_patterns() -> list[str]:
    return [fold(p) for p in os.getenv("ATLAS_TRANSCRIPT_MEETINGS", "").split(";") if p.strip()]


def transcripts_wanted() -> bool:
    return bool(meeting_patterns())


def lookback_days() -> int:
    try:
        return max(1, int(os.getenv("ATLAS_TRANSCRIPT_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)))
    except ValueError:
        return DEFAULT_LOOKBACK_DAYS


def transcripts_dir(node: str) -> Path:
    return paths.local_dir() / "transcripts" / paths.safe_name(node)


def _state_path() -> Path:
    return paths.local_dir() / "transcripts" / "state.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _dt(value: Any) -> datetime | None:
    if isinstance(value, dict):  # Graph dateTimeTimeZone {dateTime, timeZone}; asked in UTC
        value = value.get("dateTime")
    if not value:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(value).strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# WebVTT -> readable text
# ---------------------------------------------------------------------------

_TS = re.compile(r"^(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->")
_VOICE = re.compile(r"<v\s+([^>]*)>(.*?)(?:</v>|$)", re.DOTALL)
_TAG = re.compile(r"</?[^>]+>")


def _stamp(line: str) -> str:
    start = line.split("-->")[0].strip()
    parts = start.split(":")
    h, m, s = (["0", *parts] if len(parts) == 2 else parts)[:3]
    total = int(h) * 3600 + int(m) * 60 + int(float(s.replace(",", ".")))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}" if total >= 3600 else \
        f"{total // 60:02d}:{total % 60:02d}"


def vtt_to_text(vtt: str) -> str:
    """Teams WebVTT → one paragraph per speaker turn: "[mm:ss] Name: text" (consecutive cues merged)."""
    turns: list[list[str]] = []  # [stamp, speaker, text]
    stamp = ""
    for raw in vtt.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.upper().startswith(("WEBVTT", "NOTE")):
            continue
        if _TS.match(line):
            stamp = _stamp(line)
            continue
        m = _VOICE.search(line)
        speaker, text = (m.group(1).strip(), m.group(2)) if m else ("", line)
        text = " ".join(_TAG.sub("", text).split())
        if not text or (not m and re.fullmatch(r"[0-9a-f-]{8,}(/\d+-\d+)?|\d+", line)):
            continue  # a cue id
        if turns and turns[-1][1] == speaker:
            turns[-1][2] += " " + text
        else:
            turns.append([stamp, speaker, text])
    return "\n\n".join(f"[{t}] {s + ': ' if s else ''}{x}" for t, s, x in turns)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


class TranscriptsCheck:
    name = NAME

    def __init__(self, token: Any = None, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token  # () -> str (tests); default: the shared MSAL cache
        self._transport = transport

    def preflight(self, config: dict[str, Any]) -> None:
        if not transcripts_wanted():
            raise CheckNotConfigured("no meetings to follow (ATLAS_TRANSCRIPT_MEETINGS is empty)", HINT)
        if self._token is None and not os.getenv("ATLAS_MS_CLIENT_ID", "").strip():
            raise CheckNotConfigured("ATLAS_MS_CLIENT_ID is not set", HINT)

    async def _bearer(self) -> str:
        if self._token is not None:
            return self._token()
        from ..live.graphfiles import GraphFilesError, graph_token

        try:
            return await asyncio.to_thread(graph_token, TRANSCRIPT_SCOPES)
        except GraphFilesError as exc:
            raise CheckNotConfigured(f"Teams transcripts: {exc}", HINT) from exc

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None,
                   accept: str = "application/json") -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._bearer()}", "Accept": accept,
                   "Prefer": 'outlook.timezone="UTC"'}
        for attempt in range(4):
            resp = await client.get(url if url.startswith("http") else GRAPH + url, params=params, headers=headers)
            if resp.status_code in (429, 503, 504) and attempt < 3:
                await asyncio.sleep(min(float(resp.headers.get("Retry-After") or 2 ** attempt), 20))
                continue
            if resp.status_code in (401, 403):
                raise CheckNotConfigured(f"Microsoft 365 refused {url.split('?')[0]} (HTTP {resp.status_code}): "
                                         f"{_err(resp)}", HINT)
            return resp
        return resp

    async def _events(self, client: httpx.AsyncClient, start: datetime, end: datetime) -> list[dict[str, Any]]:
        pats = meeting_patterns()
        out: list[dict[str, Any]] = []
        url: str | None = "/me/calendarView"
        params: dict[str, str] | None = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": "subject,start,end,isCancelled,isOnlineMeeting,onlineMeeting", "$top": "200",
        }
        while url:
            resp = await self._get(client, url, params)
            if resp.status_code >= 400:
                raise RuntimeError(f"calendar: HTTP {resp.status_code} {_err(resp)}")
            data = resp.json()
            params = None
            for ev in data.get("value", []):
                subject = str(ev.get("subject") or "")
                join = ((ev.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
                if ev.get("isCancelled") or not join or not any(p in fold(subject) for p in pats):
                    continue
                out.append({"subject": subject.strip(), "join": join, "start": _dt(ev.get("start")),
                            "end": _dt(ev.get("end"))})
            url = data.get("@odata.nextLink")
        return out

    async def run(self, ctx: CheckContext) -> CheckResult:
        self.preflight(ctx.config)
        now = ctx.now if ctx.now.tzinfo else ctx.now.replace(tzinfo=UTC)
        start = now - timedelta(days=lookback_days())
        state = _load_state()
        saved: dict[str, str] = dict(state.get("saved") or {})
        folder = transcripts_dir(ctx.node)
        result = CheckResult()
        new_files: list[str] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=60.0, follow_redirects=True) as client:
            events = await self._events(client, start, now)
            await record_evidence(ctx, "web_fetch", "Microsoft Graph /me/calendarView",
                                  f"{len(events)} matching meeting(s) since {start:%Y-%m-%d}")
            if not events:
                result.notes.append(f"No meetings matching {', '.join(meeting_patterns())} in the last "
                                    f"{lookback_days()} days")
                return result
            meetings: dict[str, dict[str, Any]] = {}
            for ev in events:
                meetings.setdefault(ev["join"], {"subject": ev["subject"], "events": []})["events"].append(ev)
            for join, info in meetings.items():
                resp = await self._get(client, "/me/onlineMeetings", {"$filter": f"JoinWebUrl eq '{join}'"})
                items = resp.json().get("value", []) if resp.status_code < 400 else []
                if not items:
                    result.notes.append(f"{info['subject']}: the Teams meeting was not found (only meetings you "
                                        "organize or attend can be read)")
                    continue
                mid = items[0]["id"]
                resp = await self._get(client, f"/me/onlineMeetings/{mid}/transcripts")
                if resp.status_code >= 400:
                    result.notes.append(f"{info['subject']}: transcripts unavailable (HTTP {resp.status_code} "
                                        f"{_err(resp)})")
                    result.ok = False
                    continue
                transcripts = [t for t in resp.json().get("value", []) if (_dt(t.get("createdDateTime")) or now) >= start]
                for t in transcripts:
                    tid = str(t.get("id") or "")
                    if not tid or tid in saved:
                        continue
                    content = await self._get(client, f"/me/onlineMeetings/{mid}/transcripts/{tid}/content",
                                              {"$format": "text/vtt"}, accept="text/vtt")
                    if content.status_code >= 400:
                        result.notes.append(f"{info['subject']}: a transcript could not be downloaded "
                                            f"(HTTP {content.status_code})")
                        result.ok = False
                        continue
                    when = _dt(t.get("createdDateTime")) or now
                    text = vtt_to_text(content.text)
                    path = self._write(folder, info["subject"], when, text, t)
                    saved[tid] = path.name
                    new_files.append(path.name)
                    await record_evidence(ctx, "file_written", str(path), f"Teams transcript · {len(text):,} chars")
                result.alerts += self._missing(info, transcripts, now)
        state["saved"] = saved
        _save_state(state)
        if new_files:
            result.notes.append(f"Saved {len(new_files)} new transcript(s) for the agents: " + ", ".join(new_files))
        else:
            result.notes.append("No new transcripts")
        return result

    @staticmethod
    def _write(folder: Path, subject: str, when: datetime, text: str, t: dict[str, Any]) -> Path:
        from ..inbox.state import user_tz

        local = when.astimezone(user_tz())
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{local:%Y-%m-%d} {paths.safe_name(subject)[:80]}".strip()
        path, n = folder / f"{base}.txt", 2
        while path.exists():
            path = folder / f"{base} ({n}).txt"
            n += 1
        head = (f"# {subject}\nFecha: {local:%Y-%m-%d %H:%M} ({local.tzname()})\n"
                f"Fuente: transcripción de Teams (Microsoft Graph), id {t.get('id')}\n\n")
        path.write_text(head + text + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _missing(info: dict[str, Any], transcripts: list[dict[str, Any]], now: datetime) -> list[AlertDraft]:
        made = [d for t in transcripts if (d := _dt(t.get("createdDateTime")))]
        out = []
        for ev in info["events"]:
            s, e = ev.get("start"), ev.get("end")
            if not s or not e or e > now - MISSING_AFTER or e < now - MISSING_WINDOW:
                continue
            if any(s - timedelta(hours=1) <= d <= e + timedelta(hours=12) for d in made):
                continue
            from ..inbox.state import user_tz

            day = s.astimezone(user_tz()).strftime("%Y-%m-%d")
            out.append(AlertDraft(
                check=NAME, kind="other", severity="LOW",
                title=f"{info['subject']} {day}: sin transcripción",
                detail="La junta terminó y Teams no tiene transcripción. Actívala al iniciar la reunión "
                       "(Más → Grabar y transcribir → Iniciar transcripción) para que ATLAS la lea.",
                fingerprint=fingerprint(NAME, "missing", info["subject"], day),
            ))
        return out


def _err(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")[:200]
    except Exception:  # noqa: BLE001 — not JSON
        return resp.text[:200]


def make_check() -> TranscriptsCheck:
    return TranscriptsCheck()


try:
    from .engine import register_check

    register_check(NAME, TranscriptsCheck)
except ImportError:  # pragma: no cover
    pass
