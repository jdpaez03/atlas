"""Missions: create (simulated or live, JSON or multipart), history, cancel, attachments, files, thread.

Mode resolution (docs/LIVE.md):
  mode "live"       -> live orchestrator on the configured backend (ATLAS_LLM_BACKEND: api | subscription);
                       422 with the reason when no backend is usable
  mode "simulated"  -> scenario (scenario_id, or the node's first scenario)
  no mode           -> simulated if a scenario_id is given or live is unavailable, else live

Phase 3 (docs/PHASE3.md B, docs/EVENTS.md):
  GET  /missions?node=&limit=                              history, newest first (MissionSummary[])
  POST /missions                                           JSON, or multipart with files[] (≤25 MB each, ≤20)
  POST /missions/{id}/attachments                          multipart files[] -> Mission
  GET  /missions/{id}/files/{attachments|outputs}/{name}   download
  POST /missions/{id}/messages {text}                      mission thread -> the human's AgentMessage
  POST /missions/{id}/resume                               re-run failed/cancelled tasks as a new round (Phase 5)
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from ..core import paths
from ..core.models import AgentMessage, Attachment, Mission, MissionPhase, Usage, _id
from ..core.store import StoreError
from .deps import LiveDep, RegistryDep, SimDep, StoreDep, http_error

router = APIRouter(tags=["missions"])

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_MISSION = 20
_CHUNK = 1024 * 1024


class MissionCreate(BaseModel):
    objective: str = Field(min_length=1)
    node: str
    mode: Literal["simulated", "live"] | None = None
    scenario_id: str | None = None
    speed: float | None = Field(default=None, gt=0)


class MissionSummary(BaseModel):
    id: str
    objective: str
    node: str
    mode: Literal["simulated", "live"]
    phase: MissionPhase
    round: int
    interrupted: bool
    created_at: datetime
    closed_at: datetime | None
    report_versions: list[int]
    usage: Usage


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


def resolve_mode(body: MissionCreate, live_available: bool) -> str:
    if body.mode:
        return body.mode
    if body.scenario_id or not live_available:
        return "simulated"
    return "live"


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


def _unique(directory: Path, name: str) -> Path:
    path = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    return path


async def _save_uploads(mission_id: str, files: list[UploadFile], already: int) -> list[Attachment]:
    """Store uploads in the mission's attachments folder. All or nothing: on any error the files written
    by this call are removed and an HTTPException is raised (413 too large, 422 too many)."""
    if already + len(files) > MAX_FILES_PER_MISSION:
        raise HTTPException(422, f"at most {MAX_FILES_PER_MISSION} files per mission "
                                 f"(has {already}, got {len(files)})")
    directory = paths.attachments_dir(mission_id)
    written: list[Path] = []
    out: list[Attachment] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for f in files:
            path = _unique(directory, paths.safe_name(f.filename or "file"))
            written.append(path)
            size = 0
            with path.open("wb") as fh:
                while chunk := await f.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, f"'{f.filename}' is larger than "
                                                 f"{MAX_FILE_BYTES // (1024 * 1024)} MB")
                    fh.write(chunk)
            out.append(Attachment(
                name=path.name, kind="file", uri=str(path), size_bytes=size,
                download_url=f"/missions/{mission_id}/files/attachments/{quote(path.name)}",
            ))
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    finally:
        for f in files:
            await f.close()
    return out


def _upload_list(form: Any) -> list[UploadFile]:
    items = [*form.getlist("files"), *form.getlist("files[]")]
    return [f for f in items if isinstance(f, UploadFile) and (f.filename or f.size)]


def _is_multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


_CREATE_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": MissionCreate.model_json_schema()},
            "multipart/form-data": {"schema": {
                "type": "object",
                "properties": {
                    **MissionCreate.model_json_schema()["properties"],
                    "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                },
                "required": ["objective", "node"],
            }},
        },
    }
}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/missions", response_model=list[MissionSummary])
def list_missions(store: StoreDep, node: str | None = None, limit: int = 50) -> list[MissionSummary]:
    missions = [m for m in store.snapshot().missions if node is None or m.node == node]
    missions.sort(key=lambda m: m.created_at, reverse=True)
    out = []
    for m in missions[: max(0, limit)]:
        out.append(MissionSummary(
            id=m.id, objective=m.objective, node=m.node, mode=m.mode, phase=m.phase, round=m.round,
            interrupted=m.interrupted, created_at=m.created_at, closed_at=m.closed_at,
            report_versions=[r.version for r in store.mission_reports_for(m.id)], usage=m.usage,
        ))
    return out


@router.post("/missions", response_model=Mission, openapi_extra=_CREATE_OPENAPI)
async def create_mission(request: Request, sim: SimDep, live: LiveDep, registry: RegistryDep) -> Mission:
    files: list[UploadFile] = []
    try:
        if _is_multipart(request):
            form = await request.form(max_files=MAX_FILES_PER_MISSION * 2 + 10)
            raw = {k: v for k in ("objective", "node", "mode", "scenario_id", "speed")
                   if isinstance(v := form.get(k), str) and v != ""}
            files = _upload_list(form)
        else:
            raw = await request.json()
        body = MissionCreate.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    except ValueError:
        raise HTTPException(422, "the body must be JSON or multipart/form-data") from None

    enabled = {n.id for n in registry.nodes()}
    if body.node not in enabled:
        try:
            registry.agents_for_node(body.node)
        except ValueError:
            raise HTTPException(404, f"unknown node '{body.node}'") from None
        raise HTTPException(422, f"node '{body.node}' is disabled")

    info = live.backend_info()
    mode = resolve_mode(body, info.available)
    scenario = None
    if mode == "live":
        if not info.available:
            raise HTTPException(422, f"live mode is unavailable: {info.hint}")
        if body.scenario_id:
            raise HTTPException(422, "scenario_id is only valid for simulated missions")
    elif body.scenario_id:
        scenario = sim.library.get(body.scenario_id)
        if scenario is None:
            raise HTTPException(404, f"unknown scenario '{body.scenario_id}'")
        if scenario.node != body.node:
            raise HTTPException(
                422, f"scenario '{scenario.id}' belongs to node '{scenario.node}', not '{body.node}'"
            )
    else:
        scenario = sim.library.default_for(body.node)
        if scenario is None:
            raise HTTPException(422, f"no simulated scenario available for node '{body.node}'")

    # files first (under the mission id the mission will get), so agents can read them from the start
    mission_id = _id("msn")
    try:
        attachments = await _save_uploads(mission_id, files, 0) if files else []
    except BaseException:
        shutil.rmtree(paths.attachments_dir(mission_id).parent, ignore_errors=True)
        raise
    try:
        if mode == "live":
            return await live.start(body.objective, body.node, backend=info.backend, mission_id=mission_id,
                                    attachments=attachments)
        return await sim.start(scenario, objective=body.objective, node=body.node, speed=body.speed,
                               mission_id=mission_id, attachments=attachments)
    except BaseException as exc:
        if attachments:
            shutil.rmtree(paths.attachments_dir(mission_id).parent, ignore_errors=True)
        if isinstance(exc, ValueError):
            raise http_error(exc) from exc
        raise


@router.post("/missions/{mission_id}/cancel", response_model=Mission)
async def cancel_mission(mission_id: str, sim: SimDep, live: LiveDep, store: StoreDep) -> Mission:
    try:
        store.mission(mission_id)  # 404 for unknown missions
        if not (await sim.cancel(mission_id) or await live.cancel(mission_id)):
            await store.cancel_mission(mission_id)  # nothing running it: just close it (409 if closed)
        return store.mission(mission_id)
    except StoreError as exc:
        raise http_error(exc) from exc


@router.post("/missions/{mission_id}/resume", response_model=Mission)
async def resume_mission(mission_id: str, live: LiveDep, store: StoreDep) -> Mission:
    """Phase 5: re-run a closed live mission's FAILED / CANCELLED tasks (also after an interrupted run) as a new
    round with a new report version. 409 when it is running or there is nothing to resume."""
    try:
        store.mission(mission_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    try:
        return await live.resume(mission_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/missions/{mission_id}/attachments", response_model=Mission,
             openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                 "type": "object", "required": ["files"],
                 "properties": {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}},
             }}}}})
async def add_attachments(mission_id: str, request: Request, store: StoreDep) -> Mission:
    try:
        mission = store.mission(mission_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    if not _is_multipart(request):
        raise HTTPException(422, "send the files as multipart/form-data (files[])")
    form = await request.form(max_files=MAX_FILES_PER_MISSION * 2 + 10)
    files = _upload_list(form)
    if not files:
        raise HTTPException(422, "no files")
    attachments = await _save_uploads(mission_id, files, len(mission.attachments))
    return await store.add_attachments(mission_id, attachments)


@router.get("/missions/{mission_id}/files/{kind}/{name}")
def download_file(mission_id: str, kind: Literal["attachments", "outputs"], name: str,
                  store: StoreDep) -> FileResponse:
    try:
        mission = store.mission(mission_id)
    except StoreError as exc:
        raise http_error(exc) from exc
    # Names may contain spaces and parentheses ("memo (2).docx"); only reject path tricks.
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "invalid file name")
    base = paths.attachments_dir(mission.id) if kind == "attachments" else paths.outputs_dir(mission.node, mission.id)
    path = base / name
    if not paths.is_within(path, base) or not path.is_file():
        raise HTTPException(404, f"no such file '{name}'")
    return FileResponse(path.resolve(), filename=name)


@router.post("/missions/{mission_id}/messages", response_model=AgentMessage)
async def post_message(mission_id: str, body: MessageCreate, store: StoreDep, sim: SimDep,
                       live: LiveDep) -> AgentMessage:
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "the message is empty")
    try:
        mission = store.mission(mission_id)
        if mission.mode != "live":
            return await sim.post_message(mission_id, text)
        if live.mission(mission_id) is not None:  # running: guidance
            return await live.post_message(mission_id, text)
        info = live.backend_info()  # closed or interrupted: a follow-up needs a usable backend
        if not info.available:
            raise HTTPException(422, f"live mode is unavailable: {info.hint}")
        return await live.post_message(mission_id, text, backend=info.backend)
    except StoreError as exc:
        raise http_error(exc) from exc
