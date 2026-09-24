"""Inbox engine (docs/INBOX.md §2): scan the mailbox as a live mission, keep follow-ups, draft follow-ups.

A scan is a live mission "Inbox scan · <date time>" in the corporate node:

    DECOMPOSITION   list new messages (since the last scan, or ATLAS_INBOX_LOOKBACK_DAYS), skip processed ids
    DELEGATION      task "Extract follow-ups …" → HERMES
    EXECUTION       batches of ≤ 10: body fetched on demand → record_followups; evidence email_read per email
                    (subject + sender only); items merged into open follow-ups (same conversation, or same
                    counterpart + similar title) instead of duplicated
    VALIDATION      staleness pass (no LLM): THEIR_COMMITMENT past due, AWAITING_REPLY without a reply after
                    ATLAS_FOLLOWUP_DAYS business days (America/Mexico_City) → WAITING, queued for a draft;
                    then task "Draft …" → ALFRED calls draft_email per queued item (evidence draft_created)
    CONSOLIDATION → REPORTING → CLOSED   mission report with the counts and anything unreadable

Privacy: message bodies live only in local variables for the duration of one LLM call. They are never put
in events, evidence, the state file, logs or any file. Follow-ups keep an EmailRef (≤ 400-char verbatim
excerpt). ATLAS never sends email.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..core.models import (
    AgentStatus,
    Claim,
    ClaimKind,
    Confidence,
    EmailDraft,
    EmailRef,
    FollowUp,
    Mission,
    MissionPhase,
    MissionReport,
    Priority,
    TaskStatus,
)
from ..core.store import NotFoundError, StoreError, WorldStore, _person
from ..live.evidence import record
from ..live.llm import LLMError, Meter
from ..live.runtime import MissionScope
from .drafts import write_eml
from .prompts import (
    DRAFT_EMAIL_TOOL,
    DRAFT_NOTE,
    EXCERPT_MAX,
    EXTRACT_NOTE,
    KINDS,
    RECORD_FOLLOWUPS_TOOL,
    draft_message,
    extraction_message,
)
from .sources.base import MailMessage, MailSource, address_of, my_addresses, trim_quoted
from .state import InboxState, aware, business_days_between, user_tz

log = logging.getLogger("atlas.inbox")

NODE = "corporate"
HERMES = "hermes"
ALFRED = "alfred"
BATCH_SIZE = 10
MAX_DRAFTS_PER_SCAN = 10
MAX_DIGEST_DAYS = 14
LIST_LIMIT = 200
OVERLAP = timedelta(hours=1)  # re-list a little before the last scan (late-arriving mail); ids dedupe
DRAFT_CONTEXT_CHARS = 3000
OPEN_STATUSES = ("OPEN", "WAITING")
DRAFTABLE = ("THEIR_COMMITMENT", "AWAITING_REPLY")
_PRIO_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
NO_SOURCE_HINT = ("No mail source is configured: set ATLAS_MS_CLIENT_ID (Outlook / Microsoft 365) or "
                  "ATLAS_MAIL_FOLDER (a folder of exported emails) in .env — see docs/INBOX_SETUP.md")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def followup_days() -> int:
    return _env_int("ATLAS_FOLLOWUP_DAYS", 3)


def lookback_days() -> int:
    return _env_int("ATLAS_INBOX_LOOKBACK_DAYS", 3)


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def is_verbatim(excerpt: str, body: str) -> bool:
    e = _norm(excerpt)
    return bool(e) and e in _norm(body)


def similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.72:
        return True
    ta, tb = set(re.findall(r"\w{3,}", a)), set(re.findall(r"\w{3,}", b))
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6


def _parse_due(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Errors (mapped to HTTP codes by routes/inbox.py)
# ---------------------------------------------------------------------------


class InboxError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class NoSourceError(InboxError):
    """No mail source, or it is not connected (409)."""


class ScanBusyError(InboxError):
    """A scan is already running (409)."""


class LiveUnavailableError(InboxError):
    """No LLM backend (422)."""


class DraftConflictError(InboxError):
    """The draft / follow-up is not in a state that allows this (409)."""


class _MissionGone(Exception):
    """The scan mission was cancelled (closed) from outside."""


class _Meter(Meter):
    """A Meter that skips usage recording when there is no mission to charge (one-shot drafts)."""

    def _ok(self) -> bool:
        try:
            self.store.mission(self.mission_id)
            return True
        except NotFoundError:
            return False

    async def record(self, model: str, usage: Any) -> None:
        if self._ok():
            await super().record(model, usage)

    async def record_totals(self, model: str, **kw: Any) -> None:
        if self._ok():
            await super().record_totals(model, **kw)


@dataclass
class ScanCounts:
    listed: int = 0
    new_messages: int = 0
    read: int = 0
    created: int = 0
    updated: int = 0
    waiting: int = 0
    drafts: int = 0
    unreadable: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    new_titles: list[str] = field(default_factory=list)
    processed_now: list[MailMessage] = field(default_factory=list)  # new messages analyzed in this scan
    digest_candidates: int = 0  # CC emails before the automated / rules filters
    digest_threads: int = 0
    digest_skipped: int = 0
    digest_note: str | None = None  # why there is no digest


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class InboxEngine:
    def __init__(
        self,
        store: WorldStore,
        live: Any,  # LiveEngine: backend, executors, configs, loaders, llm, prices
        source: MailSource | None = None,
        *,
        source_factory: Callable[[], MailSource | None] | None = None,
        state_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        followup_days: int | None = None,
        lookback_days: int | None = None,
        batch_size: int = BATCH_SIZE,
    ):
        self.store = store
        self.live = live
        self._source = source
        self._factory = source_factory
        self.state = InboxState.load(state_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._followup_days = followup_days
        self._lookback_days = lookback_days
        self.batch_size = max(1, batch_size)
        self.tz = user_tz()
        self.scheduler: Any = None  # set by InboxScheduler
        self._scan_task: asyncio.Task[None] | None = None
        self.scan_mission_id: str | None = None
        self._drafting: set[str] = set()
        self._lock = asyncio.Lock()

    # -- configuration -------------------------------------------------------

    @property
    def source(self) -> MailSource | None:
        if self._source is not None:
            return self._source
        if self._factory is not None:
            try:
                return self._factory()
            except Exception as exc:  # noqa: BLE001 — bad ATLAS_MAIL_SOURCE etc.
                log.warning("mail source unavailable: %s", exc)
        return None

    @source.setter
    def source(self, value: MailSource | None) -> None:
        self._source = value

    @property
    def followup_days(self) -> int:
        return self._followup_days if self._followup_days is not None else followup_days()

    @property
    def lookback_days(self) -> int:
        return self._lookback_days if self._lookback_days is not None else lookback_days()

    @property
    def last_scan(self) -> datetime | None:
        return self.state.last_scan

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    def now(self) -> datetime:
        return aware(self.clock())

    def today(self) -> date:
        return self.now().astimezone(self.tz).date()

    # -- status --------------------------------------------------------------

    async def source_status(self) -> Any:
        src = self.source
        if src is None:
            return None
        try:
            return await src.status()
        except Exception as exc:  # noqa: BLE001
            from .sources.base import SourceStatus

            return SourceStatus(name=getattr(src, "name", "?"), connected=False, detail=f"Status failed: {exc}",
                                hint=getattr(exc, "hint", None))

    async def status(self) -> dict[str, Any]:
        st = await self.source_status()
        sched = self.scheduler
        nxt = sched.next_scan() if sched is not None else None
        return {
            "source": st.name if st else None,
            "connected": bool(st and st.connected),
            "account": st.account if st else None,
            "detail": st.detail if st else "No mail source configured.",
            "hint": (st.hint if st else NO_SOURCE_HINT),
            "can_draft": bool(st and st.can_draft),
            "pending": st.pending if st else None,
            "last_scan": self.state.last_scan,
            "next_scan": nxt,
            "schedule": sched.schedule_labels() if sched is not None else [],
            "processed_count": len(self.state.processed),
            "scanning": self.scanning,
            "scan_mission_id": self.scan_mission_id if self.scanning else None,
        }

    # -- scan ----------------------------------------------------------------

    async def check_ready(self) -> tuple[MailSource, str]:
        """(source, backend) or raise NoSourceError / LiveUnavailableError / ScanBusyError."""
        src = self.source
        if src is None:
            raise NoSourceError("No mail source is configured", NO_SOURCE_HINT)
        st = await self.source_status()
        if not st.connected:
            raise NoSourceError(f"The mail source ({st.name}) is not connected",
                                st.hint or st.detail or "Connect the mail source first.")
        info = self.live.backend_info()
        if not info.available:
            raise LiveUnavailableError("Live agents are unavailable", info.hint)
        if self.scanning:
            raise ScanBusyError("An inbox scan is already running", "Wait for it to finish.")
        return src, info.backend

    async def start_scan(self, trigger: str = "manual") -> Mission:
        """Create the scan mission and run it in the background. Never two scans at once."""
        async with self._lock:
            src, backend = await self.check_ready()
            local = self.now().astimezone(self.tz)
            mission = await self.store.create_mission(
                f"Inbox scan · {local.strftime('%Y-%m-%d %H:%M')}", NODE, mode="live",
                context=f"trigger: {trigger}",
            )
            self.scan_mission_id = mission.id
            await self.store.log(
                f"Inbox scan started ({trigger}) · source {src.name} · "
                f"{'your Claude plan' if backend == 'subscription' else 'the Claude API'}",
                mission_id=mission.id, agent_id=self.store.registry.orchestrator.id,
            )
            self._scan_task = asyncio.create_task(self._run_scan(mission.id, src, backend),
                                                  name=f"inbox-scan:{mission.id}")
            return mission

    async def start_digest(self, days: int = 1) -> Mission:
        """POST /digests/run: a CC digest of the last `days` days, whatever was already scanned. Shares the scan
        lock (never alongside a scan) and never touches the processed ids or last_scan."""
        days = max(1, min(MAX_DIGEST_DAYS, int(days)))
        async with self._lock:
            src, backend = await self.check_ready()
            label = f"last {days} day{'s' if days != 1 else ''}"
            mission = await self.store.create_mission(f"CC digest · {label}", NODE, mode="live",
                                                      context="trigger: digest")
            self.scan_mission_id = mission.id
            await self.store.log(
                f"CC digest started · {label} · source {src.name} · "
                f"{'your Claude plan' if backend == 'subscription' else 'the Claude API'}",
                mission_id=mission.id, agent_id=self.store.registry.orchestrator.id,
            )
            self._scan_task = asyncio.create_task(
                self._guarded(mission.id, backend, lambda scope: self._window_digest(scope, src, days)),
                name=f"inbox-digest:{mission.id}")
            return mission

    async def _window_digest(self, scope: MissionScope, src: MailSource, days: int) -> None:
        from .digest import run_digest

        s, mid = self.store, scope.mission_id
        atlas = scope.orchestrator.id
        label = f"last {days} day{'s' if days != 1 else ''}"
        end = self.now()
        start = end - timedelta(days=days)
        counts = ScanCounts()
        await scope.set_agent(atlas, AgentStatus.WORKING, activity=f"Listing email of the {label}")
        await self._phase(mid, MissionPhase.DECOMPOSITION)
        try:
            messages = await src.list_messages(since=start, limit=LIST_LIMIT)
        except Exception as exc:  # noqa: BLE001
            hint = getattr(exc, "hint", None)
            await self._abort(scope, f"Could not read the mailbox: {exc}" + (f" · {hint}" if hint else ""))
            return
        messages = sorted(messages, key=lambda m: aware(m.received_at))
        counts.listed = len(messages)
        await s.log(f"CC digest · {len(messages)} email(s) in the {label}", mission_id=mid, agent_id=atlas)
        if HERMES not in scope.agents:
            await self._abort(scope, "HERMES (agents/corporate/hermes.yaml) is not available")
            return
        await self._phase(mid, MissionPhase.EXECUTION)
        await scope.set_agent(atlas, AgentStatus.REVIEWING, activity="Supervising the CC digest")
        digest = await run_digest(self, scope, src, messages, counts, window_start=start, window_end=end,
                                  scope_label=f"in the {label}", record_reads=True)
        await self._phase(mid, MissionPhase.CONSOLIDATION)
        c = counts
        summary = (f"CC digest · {label}: {c.digest_candidates} CC candidate(s) · {c.digest_skipped} skipped · "
                   f"{c.digest_threads} thread(s)")
        if c.digest_note:
            summary += f" · {c.digest_note}"
        if c.created or c.updated:
            summary += f" · follow-ups: {c.created} new, {c.updated} updated"
        tasks = s.tasks_for(mid)
        report = MissionReport(
            mission_id=mid, executive_summary=summary,
            objective_status="PARTIAL" if c.failures else "ACHIEVED",
            tasks_completed=[t.title for t in tasks if t.status == TaskStatus.COMPLETED.value],
            tasks_pending=[t.title for t in tasks if t.status != TaskStatus.COMPLETED.value],
            key_findings=[Claim(kind=ClaimKind.FACT, statement=h, sources=["email"], confidence=Confidence.MEDIUM)
                          for h in (digest.headline if digest else [])],
            needs_human_attention=list(c.failures),
            next_actions=(["Read the CC digest in Follow-ups → Digest"] if digest else []),
            agent_report_ids=[r.id for r in s.reports_for(mid)],
        )
        await self._phase(mid, MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        await self._phase(mid, MissionPhase.FOLLOW_UP)
        await scope.set_agent(atlas, AgentStatus.COMPLETED, activity="CC digest delivered")
        await self._phase(mid, MissionPhase.CLOSED)

    async def wait(self) -> None:
        task = self._scan_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def stop(self) -> None:
        """Shutdown: stop a running scan without closing its mission (restored as interrupted)."""
        task = self._scan_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception:  # pragma: no cover
                log.exception("inbox scan raised while stopping")

    # -- scan internals ------------------------------------------------------

    def _scope(self, mission_id: str, backend: str, objective: str) -> MissionScope:
        live = self.live
        loader = live.loader_for(backend)
        agents = {}
        for aid in (HERMES, ALFRED):
            try:
                r = loader.resolve(aid)
            except Exception:  # noqa: BLE001, S112 — agent missing from a custom registry
                continue
            if r.available:
                agents[aid] = r
        return MissionScope(
            store=self.store,
            meter=_Meter(live.llm if backend == "api" else None, self.store, mission_id, live.prices),
            config=live.config_for(backend),
            context=live.context,
            mission_id=mission_id,
            objective=objective,
            node=NODE,
            agents=agents,
            orchestrator=loader.resolve(self.store.registry.orchestrator.id),
        )

    def _check_open(self, mission_id: str) -> None:
        if self.store.mission(mission_id).phase == MissionPhase.CLOSED.value:
            raise _MissionGone()

    async def _phase(self, mission_id: str, phase: MissionPhase) -> None:
        self._check_open(mission_id)
        await self.store.set_phase(mission_id, phase)

    async def _run_scan(self, mission_id: str, src: MailSource, backend: str) -> None:
        await self._guarded(mission_id, backend, lambda scope: self._scan(scope, src))

    async def _guarded(self, mission_id: str, backend: str,
                       body: Callable[[MissionScope], Awaitable[None]]) -> None:
        """Run a scan-like mission body; any failure closes the mission with a NOT_ACHIEVED report."""
        s = self.store
        scope = self._scope(mission_id, backend, s.mission(mission_id).objective)
        try:
            await body(scope)
        except asyncio.CancelledError:
            self._save_quietly()
            raise
        except _MissionGone:
            self._save_quietly()
            log.info("inbox scan %s stopped: its mission was closed", mission_id)
        except StoreError as exc:
            self._save_quietly()
            if s.mission(mission_id).phase != MissionPhase.CLOSED.value:
                log.exception("inbox scan %s failed", mission_id)
                await self._abort(scope, f"Unexpected error: {exc}")
        except Exception as exc:
            self._save_quietly()
            log.exception("inbox scan %s failed", mission_id)
            try:
                await self._abort(scope, f"Unexpected error: {exc}")
            except Exception:  # pragma: no cover
                log.exception("could not close inbox scan %s", mission_id)
        finally:
            try:
                await s.release_agents(mission_id, scope.touched)
            except Exception:  # pragma: no cover
                log.debug("release after scan failed", exc_info=True)

    async def _scan(self, scope: MissionScope, src: MailSource) -> None:
        s = self.store
        mission_id = scope.mission_id
        started = self.now()
        counts = ScanCounts()
        atlas = scope.orchestrator.id
        await scope.set_agent(atlas, AgentStatus.WORKING, activity="Listing new email")
        await self._phase(mission_id, MissionPhase.DECOMPOSITION)
        since = (self.state.last_scan - OVERLAP) if self.state.last_scan else \
            started - timedelta(days=self.lookback_days)
        oldest_retry = self.state.oldest_retry()
        if oldest_retry is not None and oldest_retry < since:
            since = oldest_retry - timedelta(minutes=1)  # re-list the messages that failed last time
        try:
            messages = await src.list_messages(since=since, limit=LIST_LIMIT)
        except Exception as exc:  # noqa: BLE001
            hint = getattr(exc, "hint", None)
            await self._abort(scope, f"Could not read the mailbox: {exc}" + (f" · {hint}" if hint else ""))
            return
        counts.listed = len(messages)
        for m in messages:
            self.state.observe(m)
        new = sorted((m for m in messages if not self.state.is_processed(m.id)),
                     key=lambda m: aware(m.received_at))
        counts.new_messages = len(new)
        await s.log(
            f"Inbox · {len(messages)} email(s) since {since.astimezone(self.tz).strftime('%Y-%m-%d %H:%M')}"
            f" · {len(new)} new", mission_id=mission_id, agent_id=atlas,
        )
        if new:
            if HERMES not in scope.agents:
                await self._abort(scope, "HERMES (agents/corporate/hermes.yaml) is not available")
                return
            await self._phase(mission_id, MissionPhase.DELEGATION)
            await self._extract_all(scope, src, new, counts)
        self.state.save()
        if counts.processed_now:
            from .digest import run_digest  # docs/INBOX.md §4

            n = len(counts.processed_now)
            await run_digest(self, scope, src, counts.processed_now, counts, window_start=since,
                             window_end=started, scope_label=f"among the {n} new email{'s' if n != 1 else ''}")

        await self._phase(mission_id, MissionPhase.VALIDATION)
        await scope.set_agent(atlas, AgentStatus.REVIEWING, activity="Checking which follow-ups are overdue")
        queue = await self._staleness(mission_id, counts)
        if queue:
            await self._draft_all(scope, src, queue, counts)

        await self._phase(mission_id, MissionPhase.CONSOLIDATION)
        await scope.set_agent(atlas, AgentStatus.WORKING, activity="Writing the scan report")
        report = self._report(mission_id, counts)
        await self._phase(mission_id, MissionPhase.REPORTING)
        await s.submit_mission_report(report)
        self.state.last_scan = started
        self.state.prune(started)
        self.state.save()
        await self._phase(mission_id, MissionPhase.FOLLOW_UP)
        await scope.set_agent(atlas, AgentStatus.COMPLETED, activity="Inbox scan delivered")
        await self._phase(mission_id, MissionPhase.CLOSED)
    def _save_quietly(self) -> None:
        try:
            self.state.save()
        except OSError:  # pragma: no cover
            log.exception("could not save the inbox state")

    async def _abort(self, scope: MissionScope, reason: str) -> None:
        s, mid = self.store, scope.mission_id
        if s.mission(mid).phase == MissionPhase.CLOSED.value:
            return
        for t in s.tasks_for(mid):
            if t.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
                await s.update_task(t.id, status=TaskStatus.CANCELLED)
        await s.log(f"Inbox scan could not complete · {reason}", mission_id=mid, agent_id=scope.orchestrator.id)
        await scope.set_agent(scope.orchestrator.id, AgentStatus.ERROR, activity=_short(reason, 120))
        await s.set_phase(mid, MissionPhase.REPORTING)
        await s.submit_mission_report(MissionReport(
            mission_id=mid, executive_summary=reason, objective_status="NOT_ACHIEVED",
            needs_human_attention=[reason],
        ))
        await s.set_phase(mid, MissionPhase.CLOSED)

    # -- extraction (HERMES) -------------------------------------------------

    async def _extract_all(self, scope: MissionScope, src: MailSource, new: list[MailMessage],
                           counts: ScanCounts) -> None:
        s, mid = self.store, scope.mission_id
        batches = [new[i:i + self.batch_size] for i in range(0, len(new), self.batch_size)]
        task = await s.create_task(
            mid, f"Extract follow-ups from {len(new)} email(s)",
            "Read the new emails and record explicit commitments, requests and questions awaiting reply.",
            HERMES, created_by=scope.orchestrator.id, priority=Priority.MEDIUM,
        )
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)
        await self._phase(mid, MissionPhase.EXECUTION)
        await scope.set_agent(scope.orchestrator.id, AgentStatus.REVIEWING, activity="Supervising HERMES")
        for i, batch in enumerate(batches):
            self._check_open(mid)
            await scope.set_agent(HERMES, AgentStatus.WORKING, task_id=task.id,
                                  activity=f"Reading {len(batch)} email(s) · batch {i + 1}/{len(batches)}")
            readable: list[tuple[MailMessage, str]] = []
            for m in batch:
                try:
                    body = trim_quoted(await src.get_body(m.id))
                except Exception as exc:  # noqa: BLE001 — not marked processed: retried next scan
                    gave_up = self.state.mark_failed(m)
                    counts.unreadable.append(f"'{_short(m.subject, 80)}' · {_person(m.sender)}"
                                             + (" (given up after several tries)" if gave_up else ""))
                    await record(s, mission_id=mid, task_id=task.id, agent_id=HERMES, kind="email_read",
                                 ref=m.subject, detail=f"from {m.sender}", ok=False,
                                 summary=f"HERMES could not read '{_short(m.subject, 80)}' · {_person(m.sender)}"
                                         f" ({type(exc).__name__})")
                    continue
                readable.append((m, body))
                counts.read += 1
                await record(s, mission_id=mid, task_id=task.id, agent_id=HERMES, kind="email_read",
                             ref=m.subject, detail=f"from {m.sender}",
                             summary=f"HERMES read '{_short(m.subject, 80)}' · {_person(m.sender)}")
            if readable:
                try:
                    items = await self._extract(scope, readable)
                except LLMError as exc:
                    counts.failures.append(f"Batch {i + 1} ({len(readable)} emails) not analyzed: {exc}")
                    for m, _ in readable:
                        self.state.mark_failed(m)
                    self.state.save()
                    await s.log(f"HERMES could not analyze batch {i + 1} · {_short(str(exc), 160)}",
                                mission_id=mid, agent_id=HERMES)
                    continue
                by_id = {m.id: (m, body) for m, body in readable}
                for item in items:
                    m, body = by_id[item["message_id"]]
                    await self._apply_item(mid, item, m, body, counts)
                for m, _ in readable:
                    self.state.mark_processed(m)
                    counts.processed_now.append(m)
                self.state.save()
            readable.clear()
            await s.update_task(task.id, progress=round(0.05 + 0.9 * (i + 1) / len(batches), 2))
        findings = [Claim(kind=ClaimKind.FACT, statement=t, sources=["email"], confidence=Confidence.HIGH)
                    for t in counts.new_titles[:20]]
        await s.submit_report(
            mid, task.id, HERMES, task.title,
            actions_taken=[f"Read {counts.read} email(s) in {len(batches)} batch(es)",
                           f"Recorded {counts.created} new and updated {counts.updated} follow-up(s)"],
            inputs_used=[f"{counts.read} email(s) from the mailbox (bodies not stored)"],
            findings=findings,
            unresolved=[f"Unreadable: {u}" for u in counts.unreadable] + counts.failures,
            confidence=Confidence.HIGH if not counts.failures else Confidence.MEDIUM,
            evidence=s.evidence_for(mid, task.id),
        )
        failed = counts.failures and not counts.read
        await s.update_task(task.id, status=TaskStatus.FAILED if failed else TaskStatus.COMPLETED)
        await scope.set_agent(HERMES, AgentStatus.COMPLETED, task_id=task.id,
                              activity=f"{counts.created} new · {counts.updated} updated follow-up(s)")

    async def _extract(self, scope: MissionScope, batch: list[tuple[MailMessage, str]]) -> list[dict[str, Any]]:
        hermes = scope.agents[HERMES]
        ids = {m.id for m, _ in batch}
        kept: list[dict[str, Any]] = []
        tries = 0

        async def validate(data: dict[str, Any] | None) -> list[str]:
            """Invalid items go back to HERMES once; on the second try they are dropped (the rest is kept)."""
            nonlocal kept, tries
            tries += 1
            if data is None:
                return ["call record_followups (with an empty items list when there is nothing to track)"]
            raw = data.get("items")
            if not isinstance(raw, list):
                return ["'items' must be a list"]
            errors: list[str] = []
            kept = []
            for n, it in enumerate(raw):
                problems = []
                if not isinstance(it, dict):
                    errors.append(f"item #{n + 1} is not an object")
                    continue
                if str(it.get("message_id") or "") not in ids:
                    problems.append(f"item #{n + 1}: message_id must be one of {', '.join(sorted(ids))}")
                if str(it.get("kind") or "").upper() not in KINDS:
                    problems.append(f"item #{n + 1}: kind must be one of {', '.join(KINDS)}")
                if not str(it.get("title") or "").strip():
                    problems.append(f"item #{n + 1}: missing title")
                errors += problems
                if not problems:
                    kept.append(it)
            if errors and tries >= 2:
                log.info("HERMES: %d invalid follow-up item(s) dropped: %s", len(errors), "; ".join(errors))
                return []
            return errors

        data = await self.live.executor(self._backend(scope)).structured(
            scope, model=hermes.model, system=[hermes.role_prompt, EXTRACT_NOTE],
            prompt=extraction_message(batch, today=self.today(), tz=self.tz), tool=RECORD_FOLLOWUPS_TOOL,
            max_tokens=scope.config.max_tokens, validate=validate, attempts=2,
        )
        if data is None:
            raise LLMError("HERMES did not return valid follow-ups")
        return kept

    @staticmethod
    def _backend(scope: MissionScope) -> str:
        return "api" if scope.meter.llm is not None else "subscription"

    def _default_counterpart(self, m: MailMessage) -> str | None:
        if m.is_from_me:
            return m.to[0] if m.to else None
        return m.sender or None

    async def _apply_item(self, mission_id: str, item: dict[str, Any], m: MailMessage, body: str,
                          counts: ScanCounts) -> FollowUp:
        kind = str(item["kind"]).upper()
        title = _short(str(item.get("title") or ""), 200)
        detail = str(item.get("detail") or "").strip()[:1000]
        counterpart = str(item.get("counterpart") or "").strip() or None
        me = my_addresses()
        if counterpart is None or (me and address_of(counterpart) in me):
            counterpart = self._default_counterpart(m)
        excerpt = str(item.get("excerpt") or "").strip()
        if not is_verbatim(excerpt, body):
            if excerpt:
                log.info("HERMES excerpt for message %s is not verbatim; dropped", m.id)
            excerpt = ""
        excerpt = excerpt[:EXCERPT_MAX]
        due = _parse_due(item.get("due"))
        prio = str(item.get("priority") or "MEDIUM").upper()
        priority = prio if prio in _PRIO_RANK else "MEDIUM"
        ref = EmailRef(message_id=m.id, subject=m.subject, sender=m.sender, received_at=m.received_at,
                       excerpt=excerpt, web_link=m.web_link)
        match = self._match(kind, counterpart, title, m)
        if match is not None:
            newer = match.source is None or match.source.received_at is None or \
                aware(match.source.received_at) <= aware(m.received_at)
            changes: dict[str, Any] = {
                "title": title if newer else match.title,
                "detail": (detail or match.detail) if newer else match.detail,
                "due": due or match.due,
                "priority": max(priority, match.priority, key=lambda p: _PRIO_RANK.get(p, 1)),
                "source": ref if newer else match.source,
                "counterpart": match.counterpart or counterpart,
            }
            if newer and match.status == "WAITING" and kind == "THEIR_COMMITMENT" and due and due >= self.today():
                changes["status"] = "OPEN"  # a new promised date: no longer overdue
            merged = match.model_copy(update=changes)
            merged = await self.store.upsert_followup(
                merged, mission_id=mission_id, agent_id=HERMES,
                summary=f"Follow-up updated · '{_short(merged.title)}' · newer email from {_person(m.sender)}",
            )
            counts.updated += 1
            return merged
        f = FollowUp(node=NODE, kind=kind, title=title, detail=detail, counterpart=counterpart, due=due,
                     priority=priority, source=ref, mission_id=mission_id)
        f = await self.store.upsert_followup(f, mission_id=mission_id, agent_id=HERMES)
        counts.created += 1
        who = f" ({_person(counterpart)})" if counterpart else ""
        counts.new_titles.append(f"{f.kind}: {title}{who}" + (f" · due {due.isoformat()}" if due else ""))
        return f

    def _match(self, kind: str, counterpart: str | None, title: str, m: MailMessage) -> FollowUp | None:
        """An open follow-up this item updates: same kind and (same conversation from another message, or a
        similar title in the same conversation / with the same counterpart)."""
        who = address_of(counterpart or "")
        for f in reversed(self.store.followups(node=NODE)):
            if f.status not in OPEN_STATUSES or f.kind != kind:
                continue
            src_id = f.source.message_id if f.source else None
            conv = self.state.conversation_of(src_id)
            same_conv = bool(m.conversation_id) and conv == m.conversation_id
            if same_conv and (src_id != m.id or similar(title, f.title)):
                return f
            if who and f.counterpart and address_of(f.counterpart) == who and similar(title, f.title):
                return f
        return None

    # -- staleness (no LLM) --------------------------------------------------

    async def _staleness(self, mission_id: str | None, counts: ScanCounts | None = None
                         ) -> list[tuple[FollowUp, str]]:
        today = self.today()
        days = self.followup_days
        queue: list[tuple[FollowUp, str]] = []
        for f in list(self.store.followups(node=NODE)):
            why: str | None = None
            sent = f.source.received_at if f.source else None
            conv = self.state.conversation_of(f.source.message_id if f.source else None)
            if f.status == "OPEN":
                if f.kind == "THEIR_COMMITMENT" and f.due and f.due < today:
                    why = f"the promised date ({f.due.isoformat()}) has passed"
                elif f.kind == "AWAITING_REPLY" and sent is not None and not self.state.reply_after(conv, sent):
                    n = business_days_between(aware(sent).astimezone(self.tz).date(), today)
                    if n >= days:
                        why = f"no reply after {n} business days"
                if why:
                    f = await self.store.upsert_followup(
                        f.model_copy(update={"status": "WAITING"}), mission_id=mission_id,
                        summary=f"Follow-up overdue · '{_short(f.title)}' · {why}",
                    )
                    if counts is not None:
                        counts.waiting += 1
            elif f.status == "WAITING" and f.kind == "AWAITING_REPLY" and self.state.reply_after(conv, sent):
                await self.store.upsert_followup(
                    f.model_copy(update={"status": "OPEN"}), mission_id=mission_id,
                    summary=f"Reply received · '{_short(f.title)}' is open again",
                )
                continue
            if f.status == "WAITING" and f.kind in DRAFTABLE and not f.draft_id:
                queue.append((f, why or "still pending"))
        return queue[:MAX_DRAFTS_PER_SCAN]

    # -- drafts (ALFRED) -----------------------------------------------------

    async def _draft_all(self, scope: MissionScope, src: MailSource, queue: list[tuple[FollowUp, str]],
                         counts: ScanCounts) -> None:
        s, mid = self.store, scope.mission_id
        if ALFRED not in scope.agents:
            counts.failures.append("ALFRED is not available: no drafts were written")
            return
        task = await s.create_task(
            mid, f"Draft {len(queue)} follow-up email(s)",
            "Write short follow-up drafts for the overdue items. Drafts only: ATLAS never sends email.",
            ALFRED, created_by=scope.orchestrator.id,
        )
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)
        written: list[str] = []
        for i, (f, why) in enumerate(queue):
            self._check_open(mid)
            await scope.set_agent(ALFRED, AgentStatus.WORKING, task_id=task.id,
                                  activity=f"Drafting follow-up {i + 1}/{len(queue)} · {_short(f.title, 60)}")
            try:
                draft = await self._draft(scope, src, f, why, task_id=task.id)
            except LLMError as exc:
                counts.failures.append(f"Draft for '{_short(f.title)}' failed: {exc}")
                await s.log(f"ALFRED could not draft '{_short(f.title)}' · {_short(str(exc), 160)}",
                            mission_id=mid, agent_id=ALFRED)
                continue
            counts.drafts += 1
            written.append(f"Draft '{draft.subject}' → {', '.join(draft.to)}")
            await s.update_task(task.id, progress=round(0.05 + 0.9 * (i + 1) / len(queue), 2))
        await s.submit_report(
            mid, task.id, ALFRED, task.title,
            actions_taken=[f"Drafted {counts.drafts} follow-up email(s) (PROPOSED, not sent)"],
            inputs_used=["overdue follow-ups and their source emails"],
            findings=[Claim(kind=ClaimKind.RECOMMENDATION, statement=w, confidence=Confidence.MEDIUM)
                      for w in written],
            unresolved=[x for x in counts.failures if x.startswith("Draft for")],
            confidence=Confidence.HIGH if len(written) == len(queue) else Confidence.MEDIUM,
            evidence=s.evidence_for(mid, task.id),
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED if written or not queue else TaskStatus.FAILED)
        await scope.set_agent(ALFRED, AgentStatus.COMPLETED, task_id=task.id,
                              activity=f"{len(written)} draft(s) ready for review")

    async def _draft(self, scope: MissionScope, src: MailSource | None, f: FollowUp, why: str, *,
                     task_id: str | None = None, record_evidence: bool = True) -> EmailDraft:
        alfred = scope.agents[ALFRED]
        to_default = [f.counterpart] if f.counterpart else []
        thread_text: str | None = None
        if src is not None and f.source is not None:
            try:
                thread_text = trim_quoted(await src.get_body(f.source.message_id), DRAFT_CONTEXT_CHARS)
            except Exception:  # noqa: BLE001 — source email gone: draft from the follow-up alone
                thread_text = None
        prompt = draft_message(f, today=self.today(), tz=self.tz, thread_text=thread_text, to=to_default,
                               why=why)
        thread_text = None
        result: dict[str, Any] = {}

        async def validate(data: dict[str, Any] | None) -> list[str]:
            if data is None:
                return ["call draft_email"]
            to = [str(x).strip() for x in (data.get("to") or []) if str(x).strip()] or to_default
            errors = []
            if not to:
                errors.append("'to' needs at least one recipient")
            if not str(data.get("subject") or "").strip():
                errors.append("missing subject")
            if not str(data.get("body") or "").strip():
                errors.append("missing body")
            result.update(to=to, cc=[str(x).strip() for x in (data.get("cc") or []) if str(x).strip()],
                          subject=str(data.get("subject") or "").strip(), body=str(data.get("body") or "").strip())
            return errors

        data = await self.live.executor(self._backend(scope)).structured(
            scope, model=alfred.model, system=[alfred.role_prompt, DRAFT_NOTE], prompt=prompt,
            tool=DRAFT_EMAIL_TOOL, max_tokens=4000, validate=validate, attempts=2,
        )
        if data is None:
            raise LLMError("ALFRED did not produce a valid draft")
        mid = scope.mission_id if self._mission_exists(scope.mission_id) else None
        draft = EmailDraft(node=f.node, followup_id=f.id, to=result["to"], cc=result["cc"],
                           subject=result["subject"], body=result["body"],
                           in_reply_to=f.source.message_id if f.source else None)
        draft = await self.store.upsert_draft(draft, mission_id=mid, agent_id=ALFRED)
        current = self.store.followup(f.id)
        await self.store.upsert_followup(current.model_copy(update={"draft_id": draft.id}), mission_id=mid,
                                         agent_id=ALFRED)
        if mid and record_evidence:
            await record(self.store, mission_id=mid, task_id=task_id, agent_id=ALFRED, kind="draft_created",
                         ref=draft.subject, detail=f"to {', '.join(draft.to)}",
                         summary=f"ALFRED drafted '{_short(draft.subject, 80)}' → "
                                 f"{', '.join(_person(x) for x in draft.to)}")
        return draft

    def _mission_exists(self, mission_id: str | None) -> bool:
        if not mission_id:
            return False
        try:
            self.store.mission(mission_id)
            return True
        except NotFoundError:
            return False

    # -- report --------------------------------------------------------------

    def _report(self, mission_id: str, c: ScanCounts) -> MissionReport:
        s = self.store
        tasks = s.tasks_for(mission_id)
        summary = (
            f"Read {c.read} email(s) ({c.new_messages} new of {c.listed} listed) · "
            f"{c.created} new follow-up(s) · {c.updated} updated · {c.waiting} now waiting · "
            f"{c.drafts} draft(s) proposed"
        )
        if c.unreadable:
            summary += f" · {len(c.unreadable)} unreadable"
        if c.digest_threads:
            summary += f"\nCC digest: {c.digest_threads} threads"
        elif c.digest_note:
            summary += f"\nCC digest: none · {c.digest_note}"
        attention = [f"Unreadable email: {u}" for u in c.unreadable] + list(c.failures)
        if c.drafts:
            attention.append(f"{c.drafts} follow-up draft(s) to review and approve (nothing was sent)")
        status = "ACHIEVED" if not c.failures and not c.unreadable else "PARTIAL"
        if c.failures and not c.read and c.new_messages:
            status = "NOT_ACHIEVED"
        return MissionReport(
            mission_id=mission_id,
            executive_summary=summary,
            objective_status=status,
            tasks_completed=[t.title for t in tasks if t.status == TaskStatus.COMPLETED.value],
            tasks_pending=[t.title for t in tasks if t.status != TaskStatus.COMPLETED.value],
            key_findings=[Claim(kind=ClaimKind.FACT, statement=f"New follow-up · {t}", sources=["email"],
                                confidence=Confidence.HIGH) for t in c.new_titles[:15]],
            needs_human_attention=attention,
            next_actions=(["Review the proposed drafts in Follow-ups"] if c.drafts else []),
            agent_report_ids=[r.id for r in s.reports_for(mission_id)],
        )

    # -- follow-ups API ------------------------------------------------------

    async def patch_followup(self, followup_id: str, changes: dict[str, Any]) -> FollowUp:
        f = self.store.followup(followup_id)
        allowed = {k: v for k, v in changes.items() if k in ("status", "due", "title", "priority")}
        if "title" in allowed and not str(allowed["title"] or "").strip():
            raise StoreError("title cannot be empty")
        updated = FollowUp.model_validate(f.model_dump() | allowed)  # enum / date validation
        return await self.store.upsert_followup(updated)

    async def draft_now(self, followup_id: str) -> EmailDraft:
        """POST /followups/{id}/draft: ALFRED drafts right now (one-shot, no mission)."""
        f = self.store.followup(followup_id)
        if f.status in ("DONE", "DISMISSED"):
            raise DraftConflictError(f"follow-up '{followup_id}' is {f.status.lower()}")
        if followup_id in self._drafting:
            raise DraftConflictError("a draft for this follow-up is already being written")
        info = self.live.backend_info()
        if not info.available:
            raise LiveUnavailableError("Live agents are unavailable", info.hint)
        self._drafting.add(followup_id)
        s = self.store
        idle = s.agent_state(ALFRED).status == s.default_status(ALFRED).value
        try:
            mission_id = f.mission_id if self._mission_exists(f.mission_id) else ""
            scope = self._scope(mission_id or "", info.backend or "api", f"Follow-up draft · {f.title}")
            if ALFRED not in scope.agents:
                raise LiveUnavailableError("ALFRED is not available")
            if idle:
                await s.set_agent_state(ALFRED, AgentStatus.WORKING, activity=f"Drafting · {_short(f.title, 60)}")
            why = "the user asked for a follow-up now"
            return await self._draft(scope, self.source, f, why, record_evidence=False)
        finally:
            self._drafting.discard(followup_id)
            if idle:
                await s.reset_agent(ALFRED)

    async def decide_draft(self, draft_id: str, decision: str, *, subject: str | None = None,
                           body: str | None = None, to: list[str] | None = None,
                           cc: list[str] | None = None) -> EmailDraft:
        """APPROVED → Outlook Drafts (when the source can) else a .eml file; DISCARDED → discarded."""
        s = self.store
        d = s.draft(draft_id)
        if d.status != "PROPOSED":
            raise DraftConflictError(f"draft '{draft_id}' is already {d.status.lower()}")
        edits: dict[str, Any] = {}
        if subject is not None and subject.strip():
            edits["subject"] = subject.strip()
        if body is not None and body.strip():
            edits["body"] = body
        if to is not None:
            edits["to"] = [x.strip() for x in to if x and x.strip()]
        if cc is not None:
            edits["cc"] = [x.strip() for x in cc if x and x.strip()]
        if decision == "DISCARDED":
            return await s.upsert_draft(d.model_copy(update=edits | {"status": "DISCARDED"}))
        if decision != "APPROVED":
            raise StoreError("decision must be APPROVED or DISCARDED")
        d = d.model_copy(update=edits)
        if not d.to:
            raise StoreError("an approved draft needs at least one recipient")
        d = await s.upsert_draft(d.model_copy(update={"status": "APPROVED"}))
        link: str | None = None
        src = self.source
        if src is not None:
            try:
                link = await src.create_outlook_draft(d)
            except Exception as exc:  # noqa: BLE001 — fall back to .eml
                log.warning("Outlook draft failed (%s); exporting .eml instead", exc)
                await s.log(f"Could not save the draft in Outlook ({_short(str(exc), 120)}) · exported as .eml",
                            agent_id=ALFRED)
                link = None
        if link:
            return await s.upsert_draft(d.model_copy(update={
                "status": "EXPORTED", "export": "outlook_drafts", "download_url": link}))
        write_eml(d, in_reply_to=self.state.internet_id_of(d.in_reply_to))
        return await s.upsert_draft(d.model_copy(update={
            "status": "EXPORTED", "export": "eml", "download_url": f"/drafts/{d.id}/eml"}))


__all__ = [
    "DraftConflictError",
    "InboxEngine",
    "InboxError",
    "LiveUnavailableError",
    "NoSourceError",
    "ScanBusyError",
    "is_verbatim",
    "similar",
]
