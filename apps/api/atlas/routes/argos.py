"""ARGOS: monitoring, Rocks and the weekly L10 brief (docs/ARGOS.md).

  GET   /argos/status                 ArgosStatus {checks: [{name, label, installed, enabled, last_run, last_ok, state,
                                      note, hint}], next_run, next_brief, running, mission_id, busy, schedule,
                                      brief_schedule, last_watch, last_brief, brief_available, config_error}
  POST  /argos/run {checks?: [..]}    run a watch now -> Mission (background). 409 when a run (ARGOS, inbox scan,
                                      CC digest) is in progress, when a requested check is not installed or lacks
                                      configuration (detail carries the hint)
  POST  /argos/brief                  build the L10 brief now -> Mission (background). 409 busy / builder missing
  GET   /alerts?status=&check=&project=   Alert[] (OPEN, then ACKNOWLEDGED, then RESOLVED; HIGH first; newest)
  PATCH /alerts/{id} {status}         OPEN | ACKNOWLEDGED | RESOLVED -> Alert
  GET   /rocks                        RockStatus[] (by id)
  POST  /rocks/reload                 re-read the Rocks source — PAGA Suite or rocks.yaml (runs the rocks check) -> RockStatus[]
  PATCH /rocks/{id} {current}         manual progress -> RockStatus (atlas.argos.rocks.update_current writes rocks.yaml;
                                      501 when that function is not installed)
  GET   /briefs?limit=                Brief[] (newest first; default 20)
  GET   /briefs/{id}                  Brief
  GET   /briefs/{id}/file             the brief's .docx
"""

from __future__ import annotations

import importlib
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..argos.engine import ArgosBusyError, ArgosEngine, ArgosError, registered_checks
from ..core import paths
from ..core.models import Alert, Brief, Mission, RockStatus
from ..core.store import StoreError
from .deps import http_error

router = APIRouter(tags=["argos"])

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def get_argos(request: Request) -> ArgosEngine:
    return request.app.state.argos


class CheckStatus(BaseModel):
    name: str
    label: str
    installed: bool
    enabled: bool
    last_run: datetime | None
    last_ok: bool | None
    state: str  # ok | partial | failed | not configured | never run | not installed
    note: str | None
    hint: str | None


class ArgosStatus(BaseModel):
    checks: list[CheckStatus]
    next_run: datetime | None
    next_brief: datetime | None
    running: bool
    mission_id: str | None
    busy: bool  # any background run (ARGOS, inbox scan, CC digest) holds the shared gate
    schedule: list[str]  # ["09:30", "16:00"] local (America/Mexico_City), weekdays
    brief_schedule: list[str]  # ["MON 07:30"]
    last_watch: datetime | None
    last_brief: datetime | None
    brief_available: bool
    config_error: str | None


class RunRequest(BaseModel):
    checks: list[str] | None = None


class AlertPatch(BaseModel):
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]


class RockPatch(BaseModel):
    current: float


def _argos_error(exc: ArgosError) -> HTTPException:
    return HTTPException(409, str(exc) + (f" — {exc.hint}" if exc.hint else ""))


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@router.get("/argos/status", response_model=ArgosStatus)
def argos_status(request: Request) -> dict[str, Any]:
    return get_argos(request).status()


@router.post("/argos/run", response_model=Mission)
async def argos_run(request: Request, body: RunRequest | None = None) -> Mission:
    try:
        return await get_argos(request).start_run((body or RunRequest()).checks, trigger="manual")
    except ArgosError as exc:
        raise _argos_error(exc) from exc


@router.post("/argos/brief", response_model=Mission)
async def argos_brief(request: Request) -> Mission:
    try:
        return await get_argos(request).start_brief(trigger="manual")
    except ArgosError as exc:
        raise _argos_error(exc) from exc


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

_STATUS_ORDER = {"OPEN": 0, "ACKNOWLEDGED": 1, "RESOLVED": 2}
_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


@router.get("/alerts", response_model=list[Alert])
def list_alerts(request: Request, status: str | None = None, check: str | None = None,
                project: str | None = None) -> list[Alert]:
    items = get_argos(request).store.alerts(status=status, check=check, project=project)
    return sorted(items, key=lambda a: (_STATUS_ORDER.get(a.status, 9), _SEV_ORDER.get(a.severity, 9),
                                        -a.last_seen.timestamp()))


@router.patch("/alerts/{alert_id}", response_model=Alert)
async def patch_alert(alert_id: str, body: AlertPatch, request: Request) -> Alert:
    try:
        return await get_argos(request).store.set_alert_status(alert_id, body.status)
    except StoreError as exc:
        raise http_error(exc) from exc


# ---------------------------------------------------------------------------
# Rocks
# ---------------------------------------------------------------------------


def _rocks_sorted(argos: ArgosEngine) -> list[RockStatus]:
    return sorted(argos.store.rocks(), key=lambda r: r.id)


async def _reload_rocks(argos: ArgosEngine) -> None:
    if "rocks" not in registered_checks():
        raise HTTPException(409, "The Rocks check is not installed — atlas/argos/rocks.py is missing")
    try:
        await argos.run(["rocks"], trigger="rocks reload")
    except ArgosError as exc:
        raise _argos_error(exc) from exc


@router.get("/rocks", response_model=list[RockStatus])
def list_rocks(request: Request) -> list[RockStatus]:
    return _rocks_sorted(get_argos(request))


@router.post("/rocks/reload", response_model=list[RockStatus])
async def reload_rocks(request: Request) -> list[RockStatus]:
    argos = get_argos(request)
    await _reload_rocks(argos)
    return _rocks_sorted(argos)


@router.patch("/rocks/{rock_id}", response_model=RockStatus)
async def patch_rock(rock_id: str, body: RockPatch, request: Request) -> RockStatus:
    argos = get_argos(request)
    try:
        mod = importlib.import_module("atlas.argos.rocks")
    except ImportError:
        mod = None
    source = getattr(mod, "rocks_source", None)
    if callable(source) and source(argos.config()[0]) == "suite":
        raise HTTPException(409, "Rocks come from PAGA Suite: the owner reports progress there (ATLAS reads it)")
    fn = getattr(mod, "update_current", None)
    if not callable(fn):
        raise HTTPException(501, "Updating a Rock is not available yet (atlas.argos.rocks.update_current)")
    try:
        result = fn(rock_id, body.current)
        if inspect.isawaitable(result):
            result = await result
    except LookupError as exc:
        raise HTTPException(404, f"unknown rock '{rock_id}'") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write rocks.yaml: {exc}") from exc
    if isinstance(result, RockStatus):
        return await argos.store.upsert_rock(result, agent_id="argos")
    try:
        await _reload_rocks(argos)
    except HTTPException as exc:
        if exc.status_code == 409 and isinstance(exc.__cause__, ArgosBusyError):
            raise HTTPException(409, "Saved to rocks.yaml; the Rock's status refreshes on the next run "
                                     f"({argos.gate.describe()})") from exc
        raise
    try:
        return argos.store.rock(rock_id)
    except StoreError as exc:
        raise http_error(exc) from exc


# ---------------------------------------------------------------------------
# briefs
# ---------------------------------------------------------------------------


@router.get("/briefs", response_model=list[Brief])
def list_briefs(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> list[Brief]:
    return get_argos(request).store.briefs(limit=limit)


@router.get("/briefs/{brief_id}", response_model=Brief)
def get_brief(brief_id: str, request: Request) -> Brief:
    try:
        return get_argos(request).store.brief(brief_id)
    except StoreError as exc:
        raise http_error(exc) from exc


def brief_file(brief: Brief) -> Path | None:
    """The .docx on disk: the deliverable's uri (absolute, or relative to ATLAS_LOCAL_DIR), else
    outputs/<node>/argos/<name>. Only paths inside ATLAS_LOCAL_DIR are served."""
    d = brief.deliverable
    if d is None:
        return None
    root = paths.local_dir()
    candidates: list[Path] = []
    if d.uri and not d.uri.startswith(("http://", "https://", "/briefs/")):
        raw = Path(d.uri.removeprefix("file://"))
        candidates.append(raw if raw.is_absolute() else root / raw)
    candidates.append(root / "outputs" / paths.safe_name(brief.node) / "argos" / Path(d.name).name)
    for p in candidates:
        if p.is_file() and paths.is_within(p, root):
            return p
    return None


@router.get("/briefs/{brief_id}/file")
def download_brief(brief_id: str, request: Request) -> FileResponse:
    try:
        brief = get_argos(request).store.brief(brief_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    path = brief_file(brief)
    if path is None:
        raise HTTPException(404, "this brief has no file on disk")
    media = DOCX if path.suffix.lower() == ".docx" else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=brief.deliverable.name if brief.deliverable else path.name)


@router.get("/briefs/{brief_id}/documents/{name}")
def download_brief_document(brief_id: str, name: str, request: Request) -> FileResponse:
    """SCRIBE's institutional PDF / deck of a brief (docs/PUBLISHING.md)."""
    from ..argos.brief import brief_dir
    from ..publish.briefs import MEDIA, document_path

    try:
        brief = get_argos(request).store.brief(brief_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    path = document_path(brief.documents, name, brief_dir(brief.node))
    if path is None:
        raise HTTPException(404, "no such document for this brief")
    return FileResponse(path, media_type=MEDIA.get(path.suffix.lower(), "application/octet-stream"), filename=path.name)


# ---------------------------------------------------------------------------
# PAGA Suite sign-in (Entra): one-time consent for the Suite API scope (argos/suite_auth.py)
# ---------------------------------------------------------------------------


@router.post("/argos/suite/connect")
async def suite_connect() -> dict[str, Any]:
    """Start the device-code sign-in for the Suite scope → {user_code, verification_uri, expires_in, message}."""
    from ..argos.suite_auth import SuiteAuthError, configured_scope, get_auth

    if not configured_scope():
        raise HTTPException(409, "ATLAS_SUITE_SCOPE is not set — add the Suite API's Entra scope to .env")
    try:
        return await get_auth().start()
    except SuiteAuthError as exc:
        raise HTTPException(409, f"{exc} — {exc.hint}" if exc.hint else str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Could not start the Microsoft sign-in: {exc}") from exc


@router.get("/argos/suite/connect/status")
async def suite_connect_status() -> dict[str, Any]:
    """{state: idle|pending|connected|failed, account?, flow?, error?, hint?}"""
    from ..argos.suite_auth import get_auth

    return get_auth().poll()
