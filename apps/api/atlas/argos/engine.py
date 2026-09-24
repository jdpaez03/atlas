"""ARGOS engine (docs/ARGOS.md, "Framework"): run checks as a mission, keep alerts, build the weekly brief.

Check registry
    register_check(name, factory)      a check module registers itself at import time (factory() -> Check)
    unregister_check(name)             (tests)
    registered_checks()                {name: factory}, after importing atlas.argos.{dashboards,l10,rocks,brief}
                                       lazily (a missing module is simply "not installed")
    register_brief_builder(fn)         async fn(ctx: CheckContext) -> Brief; else atlas.argos.brief.build_brief
    brief_builder()                    the builder or None

A check may also define `preflight(config: dict) -> None` (sync or async) that raises CheckNotConfigured without
touching the network; POST /argos/run uses it to answer 409 with the hint instead of starting a mission.

A watch is a live mission "ARGOS watch · <checks> · <YYYY-MM-DD HH:MM>" in the corporate node:

    DECOMPOSITION   load argos/watch.yaml (bad YAML -> defaults + a note)
    DELEGATION      one task per check, assigned to ARGOS
    EXECUTION       checks run one after another. Each result is applied:
                      draft fingerprint matches an OPEN/ACKNOWLEDGED alert -> updated (last_seen, evidence, …)
                      otherwise -> new Alert (first_seen = last_seen = now)
                      result.ok -> OPEN alerts of that check not seen this time -> RESOLVED ("no longer detected")
                      ACKNOWLEDGED alerts are never auto-resolved
                    CheckNotConfigured -> task FAILED, status "not configured" with the hint
                    any other exception -> task FAILED, status "failed"; the other checks continue
    CONSOLIDATION → REPORTING → CLOSED   mission report: new / updated / resolved per check, notes, failures

Only one background run at a time: the RunGate is shared with the inbox engine (scans, CC digests).
Between runs ARGOS shows MONITORING · "Watching dashboards, L10, Rocks · next check 16:00".
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.models import (
    AgentStatus,
    Alert,
    AlertEvidence,
    Brief,
    Claim,
    ClaimKind,
    Confidence,
    Mission,
    MissionPhase,
    MissionReport,
    Priority,
    TaskStatus,
)
from ..core.rungate import RunGate
from ..core.store import NotFoundError, WorldStore
from ..inbox.state import aware, user_tz
from .checks import AlertDraft, CheckContext, CheckNotConfigured, CheckResult
from .config import RunState, check_enabled, load_watch_with_error

log = logging.getLogger("atlas.argos")

NODE = "corporate"
ARGOS = "argos"
CHECK_MODULES = ("dashboards", "l10", "rocks")  # the built-in checks, in run order
BRIEF_MODULE = "brief"
LABELS = {"dashboards": "dashboards", "l10": "L10", "rocks": "Rocks"}
ALERT_KINDS = {
    "missing_report", "identical_report", "moved_date", "removed_row", "kpi_mismatch", "value_change",
    "overdue_todo", "unreported_todo", "stale_issue", "rock_failed", "rock_at_risk", "other",
}
SEVERITIES = ("LOW", "MEDIUM", "HIGH")
_SEV_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
QUOTE_MAX = 300

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CheckFactory = Callable[[], Any]
BriefBuilder = Callable[[CheckContext], Awaitable[Brief] | Brief]

_CHECKS: dict[str, CheckFactory] = {}
_BRIEF: list[BriefBuilder] = []


def register_check(name: str, factory: CheckFactory) -> None:
    """Make a check available by name (idempotent; the last registration wins)."""
    _CHECKS[name] = factory


def unregister_check(name: str) -> None:
    _CHECKS.pop(name, None)


def register_brief_builder(fn: BriefBuilder | None) -> None:
    _BRIEF[:] = [fn] if fn is not None else []


def _import(module: str) -> Any:
    full = f"{__package__}.{module}"
    if full in sys.modules:
        return sys.modules[full]
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name != full:
            log.warning("ARGOS: %s could not be imported: %s", full, exc)
        return None
    except Exception:
        log.exception("ARGOS: %s failed to import", full)
        return None


def discover() -> None:
    for m in (*CHECK_MODULES, BRIEF_MODULE):
        _import(m)


def registered_checks() -> dict[str, CheckFactory]:
    discover()
    return dict(_CHECKS)


def brief_builder() -> BriefBuilder | None:
    mod = _import(BRIEF_MODULE)
    if _BRIEF:
        return _BRIEF[0]
    fn = getattr(mod, "build_brief", None) if mod is not None else None
    return fn if callable(fn) else None


def check_order(names: set[str] | list[str]) -> list[str]:
    names = set(names)
    return [n for n in CHECK_MODULES if n in names] + sorted(n for n in names if n not in CHECK_MODULES)


def label(name: str) -> str:
    return LABELS.get(name, name)


def iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _short(text: str, n: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# ---------------------------------------------------------------------------
# Errors (mapped to HTTP codes by routes/argos.py)
# ---------------------------------------------------------------------------


class ArgosError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class ArgosBusyError(ArgosError):
    """Another run (ARGOS, an inbox scan or a CC digest) is in progress (409)."""


class CheckUnavailableError(ArgosError):
    """A requested check is not installed / not configured, or the brief builder is missing (409)."""


class _MissionGone(Exception):
    """The run's mission was closed from outside (cancelled)."""


@dataclass
class CheckOutcome:
    name: str
    state: str = "ok"  # ok | partial | failed | not configured
    new: list[Alert] = field(default_factory=list)
    updated: list[Alert] = field(default_factory=list)
    unchanged: list[Alert] = field(default_factory=list)
    resolved: list[Alert] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None

    @property
    def ran(self) -> bool:
        return self.state in ("ok", "partial")

    def counts(self) -> str:
        return (f"{len(self.new)} new, {len(self.updated)} updated, {len(self.resolved)} resolved"
                + (f", {len(self.unchanged)} unchanged" if self.unchanged else ""))

    def line(self) -> str:
        head = label(self.name)
        if self.state == "not configured":
            return f"{head}: not configured — {self.hint or self.error}"
        if self.state == "failed":
            return f"{head}: failed — {self.error}"
        tail = f" · {'; '.join(self.notes)}" if self.notes else ""
        partial = " (incomplete: nothing auto-resolved)" if self.state == "partial" else ""
        return f"{head}: {self.counts()}{partial}{tail}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ArgosEngine:
    def __init__(
        self,
        store: WorldStore,
        live: Any = None,  # LiveEngine (LLM steps); None or no backend -> ctx.scope/executor None
        *,
        mail_factory: Callable[[], Any] | None = None,
        gate: RunGate | None = None,
        clock: Callable[[], datetime] | None = None,
        state_path: Path | None = None,
        watch_path: Path | None = None,
    ):
        self.store = store
        self.live = live
        self._mail_factory = mail_factory
        self.gate = gate or RunGate()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.state = RunState.load(state_path)
        self._watch_path = watch_path
        self.tz = user_tz()
        self.scheduler: Any = None  # set by ArgosScheduler
        self._task: asyncio.Task[None] | None = None
        self.mission_id: str | None = None

    # -- basics --------------------------------------------------------------

    def now(self) -> datetime:
        return aware(self.clock())

    def local_now(self) -> datetime:
        return self.now().astimezone(self.tz)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_watch(self) -> datetime | None:
        return self.state.last_watch

    @property
    def last_brief(self) -> datetime | None:
        return self.state.last_brief

    def config(self) -> tuple[dict[str, Any], str | None]:
        return load_watch_with_error(self._watch_path)

    def enabled_checks(self, config: dict[str, Any] | None = None) -> list[str]:
        config = config if config is not None else self.config()[0]
        return check_order([n for n in registered_checks() if check_enabled(config, n)])

    def _mail(self) -> Any:
        if self._mail_factory is None:
            return None
        try:
            return self._mail_factory()
        except Exception as exc:  # noqa: BLE001
            log.warning("ARGOS: mail source unavailable: %s", exc)
            return None

    # -- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        config, cfg_err = self.config()
        installed = registered_checks()
        items = []
        for name in check_order(set(installed) | set(CHECK_MODULES)):
            run = self.state.checks.get(name)
            is_installed = name in installed
            note = run.note if run else None
            if not is_installed:
                note = f"not installed (atlas/argos/{name}.py)"
            elif not check_enabled(config, name):
                note = "disabled in argos/watch.yaml" + (f" · last: {note}" if note else "")
            items.append({
                "name": name, "label": label(name), "installed": is_installed,
                "enabled": is_installed and check_enabled(config, name),
                "last_run": run.last_run if run else None, "last_ok": run.last_ok if run else None,
                "state": (run.state if run else "never run") if is_installed else "not installed",
                "note": note, "hint": run.hint if run else None,
            })
        sched = self.scheduler
        return {
            "checks": items,
            "next_run": sched.next_run() if sched is not None else None,
            "next_brief": sched.next_brief() if sched is not None else None,
            "running": self.running,
            "mission_id": self.mission_id if self.running else None,
            "busy": self.gate.busy,
            "schedule": sched.schedule_labels() if sched is not None else [],
            "brief_schedule": sched.brief_labels() if sched is not None else [],
            "last_watch": self.state.last_watch,
            "last_brief": self.state.last_brief,
            "brief_available": brief_builder() is not None,
            "config_error": cfg_err,
        }

    def activity_line(self) -> str:
        names = self.enabled_checks()
        watching = f"Watching {', '.join(label(n) for n in names)}" if names else "No checks installed"
        nxt = self.scheduler.next_run() if self.scheduler is not None else None
        if nxt is None:
            return f"{watching} · scheduled checks off"
        local = aware(nxt).astimezone(self.tz)
        when = local.strftime("%H:%M") if local.date() == self.local_now().date() else local.strftime("%a %H:%M")
        return f"{watching} · next check {when}"

    async def set_idle(self) -> None:
        """ARGOS' steady state: MONITORING with the activity line (unless it is busy on another mission)."""
        try:
            st = self.store.agent_state(ARGOS)
        except NotFoundError:
            return
        if st.current_task_id:
            try:
                task = self.store.task(st.current_task_id)
                if task.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
                                       TaskStatus.CANCELLED.value):
                    return
            except NotFoundError:
                pass
        line = self.activity_line()
        if st.status == AgentStatus.MONITORING.value and st.activity == line and not st.current_task_id:
            return
        await self.store.set_agent_state(ARGOS, AgentStatus.MONITORING, activity=line)

    # -- starting runs -------------------------------------------------------

    async def _preflight(self, name: str, check: Any, config: dict[str, Any]) -> CheckNotConfigured | None:
        fn = getattr(check, "preflight", None)
        if fn is None:
            return None
        try:
            await _maybe_await(fn(config))
        except CheckNotConfigured as exc:
            return exc
        except Exception:
            log.debug("ARGOS: preflight of %s raised", name, exc_info=True)
        return None

    def _resolve(self, checks: list[str] | None, config: dict[str, Any]) -> list[str]:
        installed = registered_checks()
        if checks is None:
            names = self.enabled_checks(config)
            if not names:
                raise CheckUnavailableError(
                    "No ARGOS checks are installed or enabled",
                    "enable dashboards / l10 / rocks in argos/watch.yaml (docs/ARGOS.md)")
            return names
        names = list(dict.fromkeys(c.strip() for c in checks if c and c.strip()))
        if not names:
            raise CheckUnavailableError("No checks were requested", "pass checks: [dashboards, l10, rocks]")
        missing = [n for n in names if n not in installed]
        if missing:
            raise CheckUnavailableError(
                f"Check(s) not installed: {', '.join(missing)}",
                "installed: " + (", ".join(check_order(installed)) or "none"))
        return check_order(names)

    async def start_run(self, checks: list[str] | None = None, trigger: str = "manual") -> Mission:
        """Create the watch mission and run it in the background. Raises ArgosBusyError / CheckUnavailableError."""
        async with self.gate.lock:
            if self.gate.busy:
                raise ArgosBusyError(self.gate.describe(), "Wait for it to finish.")
            config, cfg_err = self.config()
            names = self._resolve(checks, config)
            factories = registered_checks()
            instances = {n: factories[n]() for n in names}
            problems = {n: exc for n in names if (exc := await self._preflight(n, instances[n], config))}
            if problems and (checks is not None or len(problems) == len(names)):
                now = self.now()
                for n, exc in problems.items():
                    self._mark(n, "not configured", now, note=f"{exc}: {exc.hint}", hint=exc.hint)
                self._save()
                first = next(iter(problems.values()))
                raise CheckUnavailableError(
                    "Not configured: " + "; ".join(f"{label(n)} ({e})" for n, e in problems.items()),
                    first.hint if len(problems) == 1 else " · ".join(e.hint for e in problems.values()))
            full = set(names) >= set(self.enabled_checks(config))
            mission = await self.store.create_mission(
                f"ARGOS watch · {', '.join(label(n) for n in names)} · {self.local_now().strftime('%Y-%m-%d %H:%M')}",
                NODE, mode="live", context=f"trigger: {trigger}",
            )
            self.mission_id = mission.id
            await self.store.log(f"ARGOS watch started ({trigger}) · {', '.join(label(n) for n in names)}",
                                 mission_id=mission.id, agent_id=self.store.registry.orchestrator.id)
            self._task = asyncio.create_task(
                self._guarded(mission.id, "watch",
                              lambda: self._watch(mission.id, names, instances, config, cfg_err, full)),
                name=f"argos-watch:{mission.id}")
            self.gate.hold("An ARGOS watch", mission.id, self._task)
            return mission

    async def run(self, checks: list[str] | None = None, trigger: str = "manual") -> Mission:
        """start_run and wait for it; returns the closed mission."""
        mission = await self.start_run(checks, trigger)
        await self.wait()
        return self.store.mission(mission.id)

    async def start_brief(self, trigger: str = "manual") -> Mission:
        """Build the weekly L10 brief as a mission (background). Raises CheckUnavailableError / ArgosBusyError."""
        builder = brief_builder()
        if builder is None:
            raise CheckUnavailableError("The weekly brief builder is not installed",
                                        "atlas/argos/brief.py (build_brief) is missing")
        async with self.gate.lock:
            if self.gate.busy:
                raise ArgosBusyError(self.gate.describe(), "Wait for it to finish.")
            config, _ = self.config()
            week = iso_week(self.local_now())
            mission = await self.store.create_mission(f"L10 brief · {week}", NODE, mode="live",
                                                      context=f"trigger: {trigger}")
            self.mission_id = mission.id
            self._task = asyncio.create_task(
                self._guarded(mission.id, "brief", lambda: self._brief(mission.id, builder, config, week)),
                name=f"argos-brief:{mission.id}")
            self.gate.hold("An L10 brief", mission.id, self._task)
            return mission

    async def wait(self) -> None:
        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def stop(self) -> None:
        """Shutdown: stop a running watch without closing its mission (restored as interrupted)."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception:  # pragma: no cover
                log.exception("ARGOS run raised while stopping")

    # -- run internals -------------------------------------------------------

    def _scope(self, mission_id: str, objective: str) -> tuple[Any, Any]:
        """(MissionScope, executor) when an LLM backend is available, else (None, None)."""
        live = self.live
        if live is None:
            return None, None
        try:
            info = live.backend_info()
            if not info.available:
                return None, None
            from ..live.llm import Meter
            from ..live.runtime import MissionScope

            backend = info.backend or "api"
            loader = live.loader_for(backend)
            agents = {}
            try:
                r = loader.resolve(ARGOS)
                if r.available:
                    agents[ARGOS] = r
            except Exception:
                log.debug("ARGOS agent unavailable for LLM steps", exc_info=True)
            scope = MissionScope(
                store=self.store,
                meter=Meter(live.llm if backend == "api" else None, self.store, mission_id, live.prices),
                config=live.config_for(backend), context=live.context, mission_id=mission_id,
                objective=objective, node=NODE, agents=agents,
                orchestrator=loader.resolve(self.store.registry.orchestrator.id),
            )
            return scope, live.executor(backend)
        except Exception:
            log.warning("ARGOS: no LLM scope for this run", exc_info=True)
            return None, None

    def _check_open(self, mission_id: str) -> None:
        if self.store.mission(mission_id).phase == MissionPhase.CLOSED.value:
            raise _MissionGone()

    async def _phase(self, mission_id: str, phase: MissionPhase) -> None:
        self._check_open(mission_id)
        await self.store.set_phase(mission_id, phase)

    def _mark(self, name: str, state: str, when: datetime, *, note: str | None, hint: str | None = None) -> None:
        run = self.state.check(name)
        run.last_run, run.state, run.note, run.hint = when, state, note, hint
        run.last_ok = state == "ok"

    def _save(self) -> None:
        try:
            self.state.save()
        except OSError:  # pragma: no cover
            log.exception("could not save the ARGOS state")

    async def _guarded(self, mission_id: str, kind: str, body: Callable[[], Awaitable[None]]) -> None:
        s = self.store
        try:
            await body()
        except asyncio.CancelledError:
            self._save()
            raise
        except _MissionGone:
            log.info("ARGOS %s %s stopped: its mission was closed", kind, mission_id)
        except Exception as exc:
            log.exception("ARGOS %s %s failed", kind, mission_id)
            try:
                await self._abort(mission_id, f"Unexpected error: {exc}")
            except Exception:  # pragma: no cover
                log.exception("could not close ARGOS mission %s", mission_id)
        finally:
            self._save()
            try:
                await s.release_agents(mission_id, [s.registry.orchestrator.id, ARGOS])
                await self.set_idle()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                pass
            except Exception:  # pragma: no cover
                log.debug("release after ARGOS run failed", exc_info=True)

    async def _abort(self, mission_id: str, reason: str) -> None:
        s = self.store
        if s.mission(mission_id).phase == MissionPhase.CLOSED.value:
            return
        for t in s.tasks_for(mission_id):
            if t.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
                await s.update_task(t.id, status=TaskStatus.CANCELLED)
        atlas = s.registry.orchestrator.id
        await s.log(f"ARGOS run could not complete · {reason}", mission_id=mission_id, agent_id=atlas)
        await s.set_agent_state(atlas, AgentStatus.ERROR, mission_id=mission_id, activity=_short(reason))
        await s.set_phase(mission_id, MissionPhase.REPORTING)
        await s.submit_mission_report(MissionReport(
            mission_id=mission_id, executive_summary=reason, objective_status="NOT_ACHIEVED",
            needs_human_attention=[reason]))
        await s.set_phase(mission_id, MissionPhase.CLOSED)

    async def _watch(self, mid: str, names: list[str], instances: dict[str, Any], config: dict[str, Any],
                     cfg_err: str | None, full: bool) -> None:
        s = self.store
        atlas = s.registry.orchestrator.id
        started = self.now()
        await s.set_agent_state(atlas, AgentStatus.WORKING, mission_id=mid, activity="Planning the ARGOS watch")
        await self._phase(mid, MissionPhase.DECOMPOSITION)
        if cfg_err:
            await s.log(f"ARGOS · {cfg_err}", mission_id=mid, agent_id=ARGOS)
        scope, executor = self._scope(mid, s.mission(mid).objective)
        await self._phase(mid, MissionPhase.DELEGATION)
        tasks = {}
        for n in names:
            tasks[n] = await s.create_task(
                mid, f"Check {label(n)}",
                f"Run the ARGOS '{n}' check: raise alerts backed by verbatim evidence, update the ones still "
                "present and let the ones no longer detected resolve.",
                ARGOS, created_by=atlas, priority=Priority.MEDIUM,
            )
        await self._phase(mid, MissionPhase.EXECUTION)
        await s.set_agent_state(atlas, AgentStatus.REVIEWING, mission_id=mid, activity="Supervising ARGOS")
        outcomes: list[CheckOutcome] = []
        for n in names:
            self._check_open(mid)
            outcomes.append(await self._run_check(mid, n, instances[n], tasks[n].id, config, scope, executor))
            self._save()

        await self._phase(mid, MissionPhase.CONSOLIDATION)
        await s.set_agent_state(atlas, AgentStatus.WORKING, mission_id=mid, activity="Writing the watch report")
        report = self._report(mid, outcomes, cfg_err)
        await self._phase(mid, MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        if full:
            self.state.last_watch = started
        self._save()
        await self._phase(mid, MissionPhase.FOLLOW_UP)
        await s.set_agent_state(atlas, AgentStatus.COMPLETED, mission_id=mid, activity="ARGOS watch delivered")
        await self._phase(mid, MissionPhase.CLOSED)

    async def _run_check(self, mid: str, name: str, check: Any, task_id: str, config: dict[str, Any],
                         scope: Any, executor: Any) -> CheckOutcome:
        s = self.store
        now = self.now()
        out = CheckOutcome(name=name)
        await s.update_task(task_id, status=TaskStatus.IN_PROGRESS, progress=0.1)
        await s.set_agent_state(ARGOS, AgentStatus.WORKING, mission_id=mid, task_id=task_id,
                                activity=f"Checking {label(name)}")
        ctx = CheckContext(store=s, scope=scope, mail=self._mail(), config=config, now=now, mission_id=mid,
                           node=NODE, task_id=task_id, executor=executor)
        result: CheckResult | None = None
        try:
            # a hung check (network, model) must not hold the shared lock and block inbox scans forever
            result = await asyncio.wait_for(check.run(ctx), timeout=_check_timeout())
            self._check_open(mid)  # cancelled while the check ran: drop its result
            if not isinstance(result, CheckResult):
                raise TypeError(f"check '{name}' returned {type(result).__name__}, not CheckResult")
        except asyncio.CancelledError:
            raise
        except _MissionGone:
            raise
        except CheckNotConfigured as exc:
            out.state, out.error, out.hint = "not configured", str(exc), exc.hint
        except TimeoutError:
            out.state, out.error = "failed", f"timed out after {int(_check_timeout())} s"
        except Exception as exc:
            self._check_open(mid)  # the mission was cancelled from outside -> _MissionGone
            log.warning("ARGOS check %s failed: %s", name, exc, exc_info=True)
            out.state, out.error = "failed", _short(f"{exc}" or type(exc).__name__, 300)
        if result is not None:
            out.notes = [str(x) for x in result.notes if str(x).strip()]
            await self._apply(mid, name, result, out, now)
            out.state = "ok" if result.ok else "partial"

        # the task's report and status
        findings = [Claim(kind=ClaimKind.FACT, statement=_alert_line(a), confidence=Confidence.HIGH,
                          sources=_sources(a)) for a in (out.new + out.updated)[:20]]
        if out.state == "not configured":
            actions, unresolved = [f"Tried the {label(name)} check"], [f"Not configured: {out.hint or out.error}"]
        elif out.state == "failed":
            actions, unresolved = [f"Tried the {label(name)} check"], [f"Failed: {out.error}"]
        else:
            actions = [f"Ran the {label(name)} check", out.counts() + " alert(s)"]
            unresolved = ["Incomplete run: open alerts were not auto-resolved"] if out.state == "partial" else []
        await s.submit_report(
            mid, task_id, ARGOS, f"Check {label(name)}", actions_taken=actions, findings=findings,
            unresolved=unresolved, limitations=out.notes,
            confidence=Confidence.HIGH if out.state == "ok" else Confidence.MEDIUM if out.ran else Confidence.LOW,
            evidence=s.evidence_for(mid, task_id),
        )
        await s.update_task(task_id, status=TaskStatus.COMPLETED if out.ran else TaskStatus.FAILED)
        await s.set_agent_state(ARGOS, AgentStatus.WORKING, mission_id=mid, task_id=task_id,
                                activity=_short(out.line(), 140))
        if out.state == "not configured":
            self._mark(name, out.state, now, note=f"{out.error}: {out.hint}" if out.hint else out.error,
                       hint=out.hint)
        elif out.state == "failed":
            self._mark(name, out.state, now, note=out.error)
        else:
            note = out.counts() + (f" · {'; '.join(out.notes)}" if out.notes else "")
            self._mark(name, out.state, now, note=_short(note, 300))
        return out

    async def _apply(self, mid: str, name: str, result: CheckResult, out: CheckOutcome, now: datetime) -> None:
        """Upsert the drafts by fingerprint; auto-resolve what a successful run no longer sees."""
        s = self.store
        seen: set[str] = set()
        for d in result.alerts:
            if not isinstance(d, AlertDraft) or not d.fingerprint:
                out.notes.append(f"ignored an invalid alert draft ({type(d).__name__})")
                continue
            fields = _draft_fields(d)
            existing = s.alert_by_fingerprint(d.fingerprint, node=NODE)
            if existing is not None and existing.id in seen:  # the same finding twice in one result
                existing = existing.model_copy(update=fields)
                await s.upsert_alert(existing, mission_id=mid, agent_id=ARGOS)
                continue
            if existing is not None:
                material = any(getattr(existing, k) != v for k, v in fields.items())
                alert = existing.model_copy(update={**fields, "last_seen": now, "mission_id": mid})
                (out.updated if material else out.unchanged).append(alert)
            else:
                alert = Alert(node=NODE, check=name, fingerprint=d.fingerprint, first_seen=now, last_seen=now,
                              mission_id=mid, **fields)
                out.new.append(alert)
            seen.add(alert.id)
            await s.upsert_alert(alert, mission_id=mid, agent_id=ARGOS)
        if not result.ok:
            return
        for a in s.alerts(status="OPEN", check=name, node=NODE):
            if a.id in seen:
                continue
            resolved = a.model_copy(update={"status": "RESOLVED"})
            await s.upsert_alert(resolved, summary=f"Resolved · {_short(a.title, 110)} (no longer detected)",
                                 mission_id=mid, agent_id=ARGOS)
            out.resolved.append(resolved)

    def _report(self, mid: str, outcomes: list[CheckOutcome], cfg_err: str | None) -> MissionReport:
        s = self.store
        new = [a for o in outcomes for a in o.new]
        updated = sum(len(o.updated) for o in outcomes)
        resolved = sum(len(o.resolved) for o in outcomes)
        ran = [o for o in outcomes if o.ran]
        not_conf = [o for o in outcomes if o.state == "not configured"]
        failed = [o for o in outcomes if o.state == "failed"]
        open_now = len(s.alerts(status="OPEN", node=NODE))
        summary = (f"ARGOS watch · {len(new)} new, {updated} updated, {resolved} resolved alert(s) · "
                   f"{open_now} open")
        if not_conf:
            summary += f" · {len(not_conf)} not configured"
        if failed:
            summary += f" · {len(failed)} failed"
        per_check = [Claim(kind=ClaimKind.FACT, statement=o.line(), sources=[o.name], confidence=Confidence.HIGH)
                     for o in outcomes]
        new_sorted = sorted(new, key=lambda a: (_SEV_RANK.get(a.severity, 9), a.project or "", a.title))
        new_claims = [Claim(kind=ClaimKind.FACT, statement=_alert_line(a), sources=_sources(a),
                            confidence=Confidence.HIGH) for a in new_sorted[:20]]
        attention = [f"New HIGH alert · {_alert_line(a)}" for a in new_sorted if a.severity == "HIGH"]
        attention += [o.line() for o in failed + not_conf]
        if cfg_err:
            attention.append(f"argos/watch.yaml: {cfg_err}")
        status = "ACHIEVED" if len(ran) == len(outcomes) else "PARTIAL" if ran else "NOT_ACHIEVED"
        tasks = s.tasks_for(mid)
        return MissionReport(
            mission_id=mid, executive_summary=summary, objective_status=status,
            tasks_completed=[t.title for t in tasks if t.status == TaskStatus.COMPLETED.value],
            tasks_pending=[t.title for t in tasks if t.status != TaskStatus.COMPLETED.value],
            key_findings=per_check + new_claims,
            needs_human_attention=attention,
            next_actions=[f"Review the {open_now} open alert(s) in Monitor"] if open_now else [],
            references=sorted({src for a in new for src in _sources(a)})[:20],
            agent_report_ids=[r.id for r in s.reports_for(mid)],
        )

    async def _brief(self, mid: str, builder: BriefBuilder, config: dict[str, Any], week: str) -> None:
        s = self.store
        atlas = s.registry.orchestrator.id
        started = self.now()
        await s.set_agent_state(atlas, AgentStatus.WORKING, mission_id=mid, activity=f"Preparing the L10 brief {week}")
        await self._phase(mid, MissionPhase.DECOMPOSITION)
        scope, executor = self._scope(mid, s.mission(mid).objective)
        await self._phase(mid, MissionPhase.DELEGATION)
        task = await s.create_task(
            mid, f"Build the L10 brief {week}",
            "Rocks, dashboards, L10 and follow-ups for the Monday meeting: every number computed by code.",
            ARGOS, created_by=atlas, priority=Priority.HIGH,
        )
        await self._phase(mid, MissionPhase.EXECUTION)
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.1)
        await s.set_agent_state(ARGOS, AgentStatus.WORKING, mission_id=mid, task_id=task.id,
                                activity=f"Writing the L10 brief {week}")
        ctx = CheckContext(store=s, scope=scope, mail=self._mail(), config=config, now=started, mission_id=mid,
                           node=NODE, task_id=task.id, executor=executor)
        try:
            brief = await _maybe_await(builder(ctx))
            if not isinstance(brief, Brief):
                raise TypeError(f"the brief builder returned {type(brief).__name__}, not Brief")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._check_open(mid)
            log.warning("ARGOS brief failed: %s", exc, exc_info=True)
            reason = f"The L10 brief could not be built: {_short(str(exc) or type(exc).__name__, 300)}"
            hint = getattr(exc, "hint", None)
            await s.submit_report(mid, task.id, ARGOS, task.title, actions_taken=["Tried to build the brief"],
                                  unresolved=[reason + (f" · {hint}" if hint else "")], confidence=Confidence.LOW,
                                  evidence=s.evidence_for(mid, task.id))
            await s.update_task(task.id, status=TaskStatus.FAILED)
            await self._abort(mid, reason + (f" · {hint}" if hint else ""))
            return
        changes: dict[str, Any] = {"mission_id": mid, "node": brief.node or NODE}
        if not brief.week:
            changes["week"] = week
        if brief.deliverable is not None and not brief.deliverable.download_url:
            changes["deliverable"] = brief.deliverable.model_copy(update={"download_url": f"/briefs/{brief.id}/file"})
        brief = brief.model_copy(update=changes)
        try:
            already = s.brief(brief.id) == brief
        except NotFoundError:
            already = False
        if not already:
            await s.upsert_brief(brief, mission_id=mid, agent_id=ARGOS)
        deliverables = [brief.deliverable] if brief.deliverable else []
        await s.submit_report(
            mid, task.id, ARGOS, task.title, actions_taken=[f"Built the L10 brief {brief.week}"],
            findings=[Claim(kind=ClaimKind.FACT, statement=h, sources=["argos"], confidence=Confidence.HIGH)
                      for h in brief.headline],
            confidence=Confidence.HIGH, evidence=s.evidence_for(mid, task.id), deliverables=deliverables,
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED)
        await s.set_agent_state(ARGOS, AgentStatus.COMPLETED, mission_id=mid, task_id=task.id,
                                activity=f"L10 brief {brief.week} ready")
        await self._phase(mid, MissionPhase.CONSOLIDATION)
        report = MissionReport(
            mission_id=mid, executive_summary=f"L10 brief {brief.week} ready"
            + (f" · {brief.headline[0]}" if brief.headline else ""),
            objective_status="ACHIEVED",
            tasks_completed=[task.title],
            key_findings=[Claim(kind=ClaimKind.FACT, statement=f"{k}: {len(v)} item(s)", sources=["argos"],
                                confidence=Confidence.HIGH) for k, v in brief.sections.items()],
            next_actions=["Read the brief in Monitor → Brief" + (" or download the .docx" if deliverables else "")],
            agent_report_ids=[r.id for r in s.reports_for(mid)],
            deliverables=deliverables,
        )
        await self._phase(mid, MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        self.state.last_brief = started
        self._save()
        await self._phase(mid, MissionPhase.FOLLOW_UP)
        await s.set_agent_state(atlas, AgentStatus.COMPLETED, mission_id=mid, activity="L10 brief delivered")
        await self._phase(mid, MissionPhase.CLOSED)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _draft_fields(d: AlertDraft) -> dict[str, Any]:
    kind = d.kind if d.kind in ALERT_KINDS else "other"
    severity = str(d.severity).upper() if str(d.severity).upper() in SEVERITIES else "MEDIUM"
    evidence = []
    for e in d.evidence or []:
        if isinstance(e, AlertEvidence):
            ev = e
        elif isinstance(e, dict):
            try:
                ev = AlertEvidence.model_validate(e)
            except Exception:  # noqa: BLE001, S112
                continue
        else:
            continue
        if len(ev.quote) > QUOTE_MAX:
            ev = ev.model_copy(update={"quote": ev.quote[: QUOTE_MAX - 1] + "…"})
        evidence.append(ev)
    return {"kind": kind, "severity": severity, "title": _short(d.title, 200) or "(untitled)",
            "detail": d.detail or "", "project": d.project or None, "evidence": evidence}


def _alert_line(a: Alert) -> str:
    return " · ".join(x for x in (a.severity, a.project, a.title) if x)


def _sources(a: Alert) -> list[str]:
    return list(dict.fromkeys(e.source for e in a.evidence))[:5] or [a.check]


def _check_timeout() -> float:
    """ATLAS_ARGOS_CHECK_TIMEOUT seconds per check (default 900)."""
    try:
        return max(0.1, float(os.getenv("ATLAS_ARGOS_CHECK_TIMEOUT", "900")))
    except ValueError:
        return 900.0
