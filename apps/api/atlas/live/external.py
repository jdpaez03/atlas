"""External agents in live missions: the `http` and `cli` adapters (contract `atlas.external/1`).

ATLAS sends the agent one request JSON and expects one report JSON back (docs/CONTRACTS.md, "External agents"):

    request  {"contract": "atlas.external/1",
              "mission": {"id", "objective", "node"},
              "task": {"id", "title", "description", "priority", "requires_approval"},
              "agent": {"id", "name"},
              "inputs": [rendered reports of the tasks this one depends on],
              "deadline_seconds": <timeout>}
    response {"asked_to"?, "actions_taken": [], "inputs_used": [],
              "findings": [{"kind", "statement", "sources": [], "confidence"}],
              "unresolved": [], "needs_agents": [], "confidence", "limitations": []}
             (optionally wrapped as {"report": {...}}; missing lists are empty, unknown kinds are ASSUMPTION,
              unknown confidence is MEDIUM)

    http -> POST the request to adapter_config.url (headers from adapter_config.headers, `${ENV}` expanded)
    cli  -> run adapter_config.command (no shell), request on stdin, report on stdout

The call itself is recorded as `external_call` evidence. What the agent says it did is self-reported, so the
report carries a limitation saying so, and its confidence is capped at MEDIUM unless every FACT has sources.
A task with `requires_approval` asks the human BEFORE the call and only calls the agent if approved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.models import (
    AgentReport,
    AgentStatus,
    ApprovalReason,
    ApprovalState,
    Claim,
    ClaimKind,
    Confidence,
    Task,
    TaskStatus,
)
from ..core.registry import expand_env
from .evidence import record
from .runtime import MissionScope, _enum, _short, _str_list, to_claims

log = logging.getLogger("atlas.live")

CONTRACT = "atlas.external/1"
DEFAULT_TIMEOUT = 300.0
SELF_REPORTED = "External agent: its actions are self-reported (ATLAS recorded only the call)"
# env vars a cli agent keeps when `inherit_env: false` (enough to start a process on every OS)
_BASE_ENV = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "LANG",
             "LC_ALL", "PATHEXT", "COMSPEC", "WINDIR")

# tests inject an httpx.MockTransport here (no network)
_transport: httpx.AsyncBaseTransport | None = None


class ExternalAgentError(RuntimeError):
    """The external agent could not be called or answered with an error: the task fails."""


@dataclass
class CallResult:
    raw: bytes
    detail: str  # evidence detail: status, duration, bytes


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


def _timeout(config: dict[str, Any]) -> float:
    try:
        value = float(config.get("timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def build_request(scope: MissionScope, task: Task, dep_reports: list[str], timeout: float) -> dict[str, Any]:
    """The request JSON of contract atlas.external/1."""
    agent = scope.agents[task.assigned_to or ""].agent
    return {
        "contract": CONTRACT,
        "mission": {"id": scope.mission_id, "objective": scope.objective, "node": scope.node},
        "task": {
            "id": task.id, "title": task.title, "description": task.description,
            "priority": str(task.priority), "requires_approval": task.requires_approval,
        },
        "agent": {"id": agent.id, "name": agent.name},
        "inputs": list(dep_reports),
        "deadline_seconds": timeout,
    }


def _expand(value: Any, what: str) -> str:
    out = expand_env(str(value))
    if "${" in out:
        raise ExternalAgentError(f"unresolved variable in {what} (set it in the environment / .env)")
    return out


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


async def call_http(config: dict[str, Any], payload: bytes, timeout: float) -> CallResult:
    url = _expand(config.get("url", ""), "url")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    for k, v in (config.get("headers") or {}).items():
        headers[str(k)] = _expand(v, f"header {k}")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
            resp = await client.post(url, content=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise ExternalAgentError(f"http agent timed out after {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        raise ExternalAgentError(f"http agent unreachable: {type(exc).__name__}: {exc}") from exc
    took = time.monotonic() - start
    if not 200 <= resp.status_code < 300:
        raise ExternalAgentError(f"http agent returned {resp.status_code}: {_short(resp.text, 200)}")
    return CallResult(resp.content, f"HTTP {resp.status_code} · {took:.1f}s · {len(resp.content)} bytes")


def command_argv(command: str) -> list[str]:
    """Split the command without a shell, then expand `${ENV}` per argument (paths with spaces stay whole)."""
    posix = os.name != "nt"
    parts = shlex.split(command, posix=posix)
    if not posix:  # shlex keeps the quotes on Windows
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    argv = [_expand(p, "command") for p in parts]
    if not argv:
        raise ExternalAgentError("cli agent: empty command")
    return argv


def _child_env(config: dict[str, Any]) -> dict[str, str] | None:
    extra = {str(k): _expand(v, f"env {k}") for k, v in (config.get("env") or {}).items()}
    inherit = config.get("inherit_env", True) is not False
    if inherit and not extra:
        return None  # same environment as ATLAS
    base = dict(os.environ) if inherit else {k: v for k, v in os.environ.items() if k.upper() in _BASE_ENV}
    return base | extra


async def call_cli(config: dict[str, Any], payload: bytes, timeout: float) -> CallResult:
    argv = command_argv(str(config.get("command", "")))
    cwd = _expand(config["cwd"], "cwd") if config.get("cwd") else None
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser(cwd) if cwd else None, env=_child_env(config),
        )
    except OSError as exc:
        raise ExternalAgentError(f"cli agent could not start: {exc.strerror or exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        await _kill(proc)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ExternalAgentError(f"cli agent timed out after {timeout:g}s (killed)") from exc
    took = time.monotonic() - start
    if proc.returncode != 0:
        excerpt = _short(err.decode("utf-8", "replace"), 200) or "(no stderr)"
        raise ExternalAgentError(f"cli agent exited with code {proc.returncode}: {excerpt}")
    return CallResult(out, f"exit 0 · {took:.1f}s · {len(out)} bytes")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (TimeoutError, asyncio.CancelledError):
        pass


ADAPTERS = {"http": call_http, "cli": call_cli}


# ---------------------------------------------------------------------------
# response
# ---------------------------------------------------------------------------


def parse_response(raw: bytes) -> dict[str, Any] | None:
    """The report object, or None when the output isn't a JSON object. Accepts {"report": {...}} and, for
    scripts that log first, a JSON object on the last non-empty line."""
    text = raw.decode("utf-8", "replace").strip()
    candidates = [text] + [line for line in reversed(text.splitlines()) if line.strip()][:1]
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, dict):
            inner = data.get("report")
            return inner if isinstance(inner, dict) else data
    return None


def _cap(confidence: Confidence, findings: list[Claim]) -> Confidence:
    """Self-reported work: HIGH only when every FACT names its sources."""
    facts_sourced = all(c.sources for c in findings if c.kind == ClaimKind.FACT.value)
    if confidence == Confidence.HIGH and not facts_sourced:
        return Confidence.MEDIUM
    return confidence


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def run_external(scope: MissionScope, task: Task, dep_reports: list[str]) -> AgentReport:
    """Run one task on an external (http / cli) agent and submit its report. Raises on a failed call."""
    s = scope.store
    resolved = scope.agents[task.assigned_to or ""]
    agent = resolved.agent
    adapter = str(agent.adapter)
    call = ADAPTERS.get(adapter)
    if call is None:
        raise ExternalAgentError(f"adapter '{adapter}' is not supported for external agents")
    config = agent.adapter_config
    timeout = _timeout(config)
    ref = str(config.get("url") if adapter == "http" else config.get("command") or "")  # as written: no secrets

    async def activity(text: str, status: AgentStatus = AgentStatus.WORKING) -> None:
        await scope.set_agent(agent.id, status, activity=text, task_id=task.id)

    async def evidence(kind: str, ref_: str, detail: str, ok: bool, summary: str | None = None) -> None:
        await record(s, mission_id=scope.mission_id, task_id=task.id, agent_id=agent.id, kind=kind, ref=ref_,
                     detail=detail, ok=ok, summary=summary)

    await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)

    if task.requires_approval and not await _approve(scope, task, activity, evidence, adapter, ref):
        await activity("Drafting report")
        report = await s.submit_report(
            scope.mission_id, task.id, agent.id, task.title,
            actions_taken=["Did not call the external agent: the human rejected the task"],
            findings=[Claim(kind=ClaimKind.FACT, statement=f"'{task.title}' was not executed: approval rejected.",
                            sources=["approval"], confidence=Confidence.HIGH)],
            unresolved=[f"'{task.title}' needs a different approach or a new approval"],
            confidence=Confidence.LOW,
            limitations=["Not executed: the human rejected the approval"],
            evidence=s.evidence_for(scope.mission_id, task.id),
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED)
        return report

    await activity(f"Calling {adapter} agent · '{_short(task.title, 60)}'")
    await s.update_task(task.id, progress=0.2)
    payload = json.dumps(build_request(scope, task, dep_reports, timeout), ensure_ascii=False).encode("utf-8")
    label = "called" if adapter == "http" else "ran"
    try:
        result = await call(config, payload, timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        err = str(exc) if isinstance(exc, ExternalAgentError) else f"{type(exc).__name__}: {exc}"
        await evidence("external_call", ref, err, False,
                       summary=_short(f"{agent.name} {label} {adapter} agent · failed: {err}", 200))
        if isinstance(exc, ExternalAgentError):
            raise
        raise ExternalAgentError(err) from exc
    await evidence("external_call", ref, result.detail, True,
                   summary=_short(f"{agent.name} {label} {adapter} agent · {result.detail}", 200))
    await activity("Drafting report")
    return await _submit(scope, task, parse_response(result.raw), result.raw)


async def _approve(scope: MissionScope, task: Task, activity: Any, evidence: Any, adapter: str, ref: str) -> bool:
    """Ask the human before calling the agent (it can't ask mid-call). True = approved."""
    s = scope.store
    agent = scope.agents[task.assigned_to or ""].agent
    reason = task.approval_reason or ApprovalReason.CONSEQUENTIAL_DECISION
    title = _short(task.title, 120)
    approval = await s.request_approval(
        scope.mission_id, agent.id, reason, title, task.description or title,
        proposed_action=f"Send this task to the external {adapter} agent {agent.name} ({_short(ref, 120)})",
        task_id=task.id,
    )
    await s.update_task(task.id, status=TaskStatus.AWAITING_APPROVAL)
    await activity("Awaiting human approval", AgentStatus.WAITING)
    decided = await s.wait_for_decision(approval.id)
    approved = decided.state == ApprovalState.APPROVED.value
    note = f" · {decided.decision_note}" if decided.decision_note else ""
    await evidence("approval", approval.id, f"{decided.state}: {title}{note}",
                   decided.state in (ApprovalState.APPROVED.value, ApprovalState.REJECTED.value))
    if s.task(task.id).status == TaskStatus.AWAITING_APPROVAL.value:
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    await activity(f"{'Approved' if approved else 'Rejected'} · {title}")
    return approved


async def _submit(scope: MissionScope, task: Task, data: dict[str, Any] | None, raw: bytes) -> AgentReport:
    s = scope.store
    agent_id = task.assigned_to or ""
    evidence = s.evidence_for(scope.mission_id, task.id)
    data = data or {}
    findings = to_claims(data.get("findings"))
    actions = _str_list(data.get("actions_taken"))
    limitations = _str_list(data.get("limitations"))
    if not findings and not actions:  # not JSON, or nothing usable in it: wrap, don't crash
        text = raw.decode("utf-8", "replace").strip()
        statement = (_short(text, 2000) if text else "The external agent returned no output.")
        report = await s.submit_report(
            scope.mission_id, task.id, agent_id, task.title,
            actions_taken=["Called the external agent; its response had no structured report"],
            findings=[Claim(kind=ClaimKind.ASSUMPTION, statement=statement, confidence=Confidence.LOW)],
            unresolved=["The external agent's response was not a report with findings (contract atlas.external/1)"],
            confidence=Confidence.LOW,
            limitations=[*limitations, "Auto-wrapped: the response was not a valid atlas.external/1 report",
                         SELF_REPORTED],
            evidence=evidence,
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED)
        return report
    confidence = _cap(_enum(data.get("confidence"), Confidence, Confidence.MEDIUM), findings)
    report = await s.submit_report(
        scope.mission_id, task.id, agent_id,
        str(data.get("asked_to") or task.title).strip(),
        actions_taken=actions,
        inputs_used=_str_list(data.get("inputs_used")),
        findings=findings,
        unresolved=_str_list(data.get("unresolved")),
        needs_agents=_str_list(data.get("needs_agents")),
        confidence=confidence,
        limitations=[*limitations, SELF_REPORTED],
        evidence=evidence,
    )
    await s.update_task(task.id, status=TaskStatus.COMPLETED)
    return report
