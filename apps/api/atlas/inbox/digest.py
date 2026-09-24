"""CC digest (docs/INBOX.md §4): a briefing on the emails where the user is only in CC.

    candidates   the scan's new (analyzed) messages where one of my addresses (ATLAS_MAIL_ME + the signed-in
                 account) is in cc and none is in to / the sender; plus, via the rules, messages TO me from
                 `include_to_from` senders. Automated mail and `exclude_senders` / keyword rules are skipped.
    rules        <ATLAS_LOCAL_DIR>/inbox/digest_rules.yaml (private; a commented example is written when missing;
                 bad YAML → logged, defaults used)
    threads      grouped by conversation_id, else by normalized subject (RE:/RV:/FW:/FWD: stripped), newest
                 first, at most `max_threads`; tagged with a project by keyword
    summaries    HERMES `summarize_threads` per ≤ 8 threads (both backends, executor.structured); headlines merged
                 across batches (≤ 3); figures whose numbers are not in the emails are dropped; `asks_me` creates
                 or updates a REQUEST_TO_ME follow-up (linked by followup_id)

Privacy: bodies are fetched on demand and only live in memory during the LLM call. The digest keeps the model's
summaries and EmailRefs without excerpts (an excerpt would be raw body text).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..core import paths
from ..core.models import (
    AgentStatus,
    Claim,
    ClaimKind,
    Confidence,
    Digest,
    DigestThread,
    EmailRef,
    Priority,
    TaskStatus,
)
from ..live.llm import LLMError
from .prompts import DIGEST_NOTE, SUMMARIZE_THREADS_TOOL, digest_message
from .sources.base import MailMessage, address_of, my_addresses, trim_quoted

if TYPE_CHECKING:
    from ..live.runtime import MissionScope
    from .engine import InboxEngine, ScanCounts
    from .sources.base import MailSource

log = logging.getLogger("atlas.inbox")

BATCH_THREADS = 8
MAX_SUMMARY = 5
MAX_HEADLINE = 3
_PRIO_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

RULES_EXAMPLE = """\
# ATLAS · CC digest rules (docs/INBOX.md §4). Private: this file lives in ATLAS_LOCAL_DIR, never in the repo.
# Every key is optional. Matching is case-insensitive. Uncomment and edit; ATLAS reads it at every scan.

# Also digest emails sent TO you by these senders (address or domain substrings),
# e.g. a board member's weekly update:
# include_to_from: [consejero@empresa.com]

# Never digest CC emails from these senders (address or domain substrings):
# exclude_senders: [sistemas@empresa.com, "@proveedor-spam.com"]

# If set, keep only CC emails whose subject or body contains one of these words:
# include_keywords: [obra, crédito, presupuesto]

# Drop CC emails whose subject or body contains one of these words:
# exclude_keywords: [cumpleaños, comida]

# Tag conversations with a project when the subject or body contains one of its keywords:
# projects:
#   Polanco: [polanco, torre p]
#   Santa Fe: [santa fe, sfe]

# Most conversations per digest (the most recent are kept):
# max_threads: 25
"""


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass
class DigestRules:
    include_to_from: list[str] = field(default_factory=list)
    exclude_senders: list[str] = field(default_factory=list)
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    projects: dict[str, list[str]] = field(default_factory=dict)
    max_threads: int = 25


def rules_path() -> Path:
    return paths.local_dir() / "inbox" / "digest_rules.yaml"


def _str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        log.warning("digest_rules.yaml: '%s' must be a list; ignored", key)
        return []
    return [str(x).strip().lower() for x in value if str(x).strip()]


def load_rules(path: Path | None = None) -> DigestRules:
    """The user's rules, or the defaults. Writes the commented example when the file is missing."""
    path = path or rules_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(RULES_EXAMPLE, encoding="utf-8")
        except OSError as exc:
            log.warning("could not write the example digest rules %s: %s", path, exc)
        return DigestRules()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("digest rules %s are unreadable (%s); using the defaults", path, exc)
        return DigestRules()
    if raw is None:
        return DigestRules()
    if not isinstance(raw, dict):
        log.warning("digest rules %s must be a mapping; using the defaults", path)
        return DigestRules()
    projects: dict[str, list[str]] = {}
    raw_projects = raw.get("projects") or {}
    if isinstance(raw_projects, dict):
        for name, words in raw_projects.items():
            kws = _str_list(words, f"projects.{name}")
            if str(name).strip() and kws:
                projects[str(name).strip()] = kws
    else:
        log.warning("digest_rules.yaml: 'projects' must be a mapping; ignored")
    try:
        max_threads = max(1, int(raw.get("max_threads") or 25))
    except (TypeError, ValueError):
        log.warning("digest_rules.yaml: 'max_threads' must be a number; using 25")
        max_threads = 25
    return DigestRules(
        include_to_from=_str_list(raw.get("include_to_from"), "include_to_from"),
        exclude_senders=_str_list(raw.get("exclude_senders"), "exclude_senders"),
        include_keywords=_str_list(raw.get("include_keywords"), "include_keywords"),
        exclude_keywords=_str_list(raw.get("exclude_keywords"), "exclude_keywords"),
        projects=projects,
        max_threads=max_threads,
    )


# ---------------------------------------------------------------------------
# Selection and grouping (pure)
# ---------------------------------------------------------------------------

_AUTOMATED_SENDER = re.compile(
    r"no-?reply|do-?not-?reply|notifications?\b|notificaciones|mailer-daemon|postmaster|bounces?\b|newsletter|"
    r"^news@|^marketing@|^calendar|^alerts?@|^automated|^digest@|^info@.*\.(?:mailchimp|sendgrid)",
    re.IGNORECASE,
)
_AUTOMATED_SUBJECT = re.compile(
    r"^\s*(?:accepted|declined|tentative|canceled|cancelled|updated invitation|invitation|aceptado|rechazado|"
    r"provisional|cancelad[oa]|invitaci[oó]n(?: actualizada)?)\s*:|automatic reply|respuesta autom[aá]tica|"
    r"out of office|fuera de la oficina|unsubscribe|darse de baja",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^\s*(?:re|rv|fw|fwd|res|enc|tr)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)


def me_addresses(account: str | None) -> set[str]:
    me = set(my_addresses())
    if account and "@" in account:
        me.add(address_of(account))
    return me


def is_automated(m: MailMessage) -> bool:
    return bool(_AUTOMATED_SENDER.search(address_of(m.sender)) or _AUTOMATED_SUBJECT.search(m.subject or ""))


def classify(m: MailMessage, me: set[str], rules: DigestRules) -> str | None:
    """'cc' (only in CC), 'to_from' (to me from an include_to_from sender) or None."""
    sender = address_of(m.sender)
    if not me or m.is_from_me or sender in me:
        return None
    to = {address_of(x) for x in m.to}
    cc = {address_of(x) for x in m.cc}
    if to & me:
        if any(p in sender for p in rules.include_to_from):
            return "to_from"
        return None
    return "cc" if cc & me else None


def normalize_subject(subject: str) -> str:
    s = subject or ""
    while True:
        stripped = _PREFIX.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    return " ".join(s.split()) or "(no subject)"


def thread_key(m: MailMessage) -> str:
    return m.conversation_id or "subject:" + normalize_subject(m.subject).casefold()


def _contains_any(text: str, words: list[str]) -> bool:
    low = text.casefold()
    return any(w.casefold() in low for w in words)


def project_for(text: str, rules: DigestRules) -> str | None:
    for name, words in rules.projects.items():
        if _contains_any(text, words):
            return name
    return None


_NUM = re.compile(r"\d(?:[\d.,:/]*\d)?")


def figure_ok(figure: str, source: str) -> bool:
    """A figure survives when it appears verbatim, or when every number in it appears as-is in the source
    (the words around the number may be the model's)."""
    norm_src = " ".join(source.split()).casefold()
    if " ".join(figure.split()).casefold() in norm_src:
        return True
    nums = _NUM.findall(figure)
    return bool(nums) and all(re.search(rf"(?<![\d]){re.escape(n)}(?![\d])", source) for n in nums)


def merge_headlines(batches: list[list[str]]) -> list[str]:
    """Round-robin across batches (each batch lists its most important first), de-duplicated, ≤ 3."""
    out: list[str] = []
    for i in range(max((len(b) for b in batches), default=0)):
        for b in batches:
            if i < len(b):
                line = " ".join(str(b[i]).split())
                if line and line.casefold() not in {x.casefold() for x in out}:
                    out.append(line)
    return out[:MAX_HEADLINE]


def _strs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [" ".join(str(x).split()) for x in (value or []) if str(x).strip()] if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# The digest step of a scan
# ---------------------------------------------------------------------------


async def run_digest(engine: InboxEngine, scope: MissionScope, src: MailSource, counts: ScanCounts, *,
                     window_start: datetime | None, window_end: datetime | None) -> Digest | None:
    s, mid = engine.store, scope.mission_id
    st = await engine.source_status()
    me = me_addresses(st.account if st else None)
    rules = load_rules()
    picked: list[MailMessage] = []
    skipped = 0
    for m in counts.processed_now:
        if classify(m, me, rules) is None:
            continue
        sender = address_of(m.sender)
        if is_automated(m) or any(p in sender for p in rules.exclude_senders):
            skipped += 1
            continue
        picked.append(m)
    counts.digest_skipped = skipped
    if not picked:
        return None
    engine._check_open(mid)
    bodies: dict[str, str] = {}
    kept: list[MailMessage] = []
    for m in picked:
        try:
            body = trim_quoted(await src.get_body(m.id))
        except Exception:  # noqa: BLE001 — already analyzed once; leave it out of the digest
            log.info("digest: message %s unreadable; left out", m.id)
            continue
        text = f"{m.subject}\n{body}"
        if (rules.include_keywords and not _contains_any(text, rules.include_keywords)) or \
                _contains_any(text, rules.exclude_keywords):
            skipped += 1
            continue
        bodies[m.id] = body
        kept.append(m)
    counts.digest_skipped = skipped
    if not kept:
        return None

    groups: dict[str, list[MailMessage]] = {}
    for m in sorted(kept, key=lambda x: x.received_at):
        groups.setdefault(thread_key(m), []).append(m)
    ordered = sorted(groups.items(), key=lambda kv: kv[1][-1].received_at, reverse=True)[: rules.max_threads]
    threads: list[dict[str, Any]] = []
    for key, msgs in ordered:
        text = "\n".join(f"{m.subject}\n{bodies[m.id]}" for m in msgs)
        threads.append({
            "conversation_id": msgs[0].conversation_id or key.removeprefix("subject:"),
            "subject": normalize_subject(msgs[-1].subject),
            "project": project_for(text, rules),
            "messages": [(m, bodies[m.id]) for m in msgs],
        })

    task = await s.create_task(
        mid, f"CC digest · {len(threads)} conversation(s)",
        "Summarize the conversations where the user is only in CC: what happened, decisions, figures, asks.",
        "hermes", created_by=scope.orchestrator.id, priority=Priority.LOW,
    )
    await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)
    batches = [threads[i:i + BATCH_THREADS] for i in range(0, len(threads), BATCH_THREADS)]
    headlines: list[list[str]] = []
    answers: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for i, batch in enumerate(batches):
        engine._check_open(mid)
        await scope.set_agent("hermes", AgentStatus.WORKING, task_id=task.id,
                              activity=f"CC digest · {len(batch)} conversation(s) · batch {i + 1}/{len(batches)}")
        try:
            data = await _summarize(engine, scope, batch)
        except LLMError as exc:
            failures.append(f"CC digest batch {i + 1} not summarized: {exc}")
            await s.log(f"HERMES could not summarize CC batch {i + 1} · {str(exc)[:160]}", mission_id=mid,
                        agent_id="hermes")
            continue
        headlines.append(_strs(data.get("headline"))[:MAX_HEADLINE])
        for t in data.get("threads") or []:
            answers[str(t.get("conversation_id"))] = t
        await s.update_task(task.id, progress=round(0.05 + 0.9 * (i + 1) / len(batches), 2))

    out: list[DigestThread] = []
    asks = 0
    for t in threads:
        a = answers.get(t["conversation_id"], {})
        msgs: list[MailMessage] = [m for m, _ in t["messages"]]
        source = "\n".join(f"{m.subject}\n{b}" for m, b in t["messages"])
        figures = [f for f in _strs(a.get("figures")) if figure_ok(f, source)]
        dropped = len(_strs(a.get("figures"))) - len(figures)
        if dropped:
            log.info("digest: %d figure(s) not found verbatim in conversation %s; dropped", dropped,
                     t["conversation_id"])
        imp = str(a.get("importance") or "MEDIUM").upper()
        importance = imp if imp in _PRIO_RANK else "MEDIUM"
        asks_me = " ".join(str(a.get("asks_me") or "").split()) or None
        followup_id = None
        if asks_me:
            last = next((m for m in reversed(msgs) if not m.is_from_me), msgs[-1])
            f = await engine._apply_item(mid, {
                "kind": "REQUEST_TO_ME", "title": asks_me, "priority": importance,
                "detail": f"Asked in a CC conversation: {t['subject']}",
                "counterpart": last.sender,
            }, last, "", counts)
            followup_id = f.id
            asks += 1
        participants: list[str] = []
        for m in msgs:
            for p in (m.sender, *m.to):
                if p and address_of(p) not in me and p not in participants:
                    participants.append(p)
        out.append(DigestThread(
            conversation_id=t["conversation_id"], subject=t["subject"], project=t["project"],
            participants=participants[:10], summary=_strs(a.get("summary"))[:MAX_SUMMARY],
            decisions=_strs(a.get("decisions")), figures=figures, asks_me=asks_me, importance=importance,
            messages=[EmailRef(message_id=m.id, subject=m.subject, sender=m.sender, received_at=m.received_at,
                               web_link=m.web_link) for m in msgs],
            followup_id=followup_id,
        ))
        t["messages"] = []  # drop the bodies
    bodies.clear()
    out.sort(key=lambda d: (-_PRIO_RANK.get(d.importance, 1),
                            -max(m.received_at.timestamp() for m in d.messages if m.received_at)))
    digest = Digest(mission_id=mid, window_start=window_start, window_end=window_end,
                    headline=merge_headlines(headlines), threads=out, skipped=skipped)
    digest = await s.upsert_digest(digest, mission_id=mid, agent_id="hermes")
    counts.digest_threads = len(out)
    counts.failures += failures
    await s.submit_report(
        mid, task.id, "hermes", task.title,
        actions_taken=[f"Summarized {len(out)} CC conversation(s) from {len(kept)} email(s)",
                       f"{asks} conversation(s) ask the user for something (follow-ups)"],
        inputs_used=[f"{len(kept)} CC email(s) (bodies not stored)"],
        findings=[Claim(kind=ClaimKind.FACT, statement=h, sources=["email"], confidence=Confidence.MEDIUM)
                  for h in digest.headline],
        unresolved=failures,
        confidence=Confidence.HIGH if not failures else Confidence.MEDIUM,
        evidence=s.evidence_for(mid, task.id),
    )
    await s.update_task(task.id, status=TaskStatus.FAILED if failures and not answers else TaskStatus.COMPLETED)
    await scope.set_agent("hermes", AgentStatus.COMPLETED, task_id=task.id,
                          activity=f"CC digest · {len(out)} conversation(s)")
    return digest


async def _summarize(engine: InboxEngine, scope: MissionScope, batch: list[dict[str, Any]]) -> dict[str, Any]:
    hermes = scope.agents["hermes"]
    ids = {t["conversation_id"] for t in batch}
    tries = 0
    kept: dict[str, Any] = {}

    async def validate(data: dict[str, Any] | None) -> list[str]:
        """Unknown conversation ids go back to HERMES once; on the second try they are dropped."""
        nonlocal tries, kept
        tries += 1
        if data is None:
            return ["call summarize_threads"]
        raw = data.get("threads")
        if not isinstance(raw, list):
            return ["'threads' must be a list"]
        errors: list[str] = []
        good = []
        for n, t in enumerate(raw):
            if not isinstance(t, dict) or str(t.get("conversation_id") or "") not in ids:
                errors.append(f"thread #{n + 1}: conversation_id must be one of {', '.join(sorted(ids))}")
                continue
            good.append(t)
        kept = {"headline": data.get("headline") or [], "threads": good}
        if errors and tries >= 2:
            return []
        return errors

    data = await engine.live.executor(engine._backend(scope)).structured(
        scope, model=hermes.model, system=[hermes.role_prompt, DIGEST_NOTE],
        prompt=digest_message(batch, today=engine.today(), tz=engine.tz), tool=SUMMARIZE_THREADS_TOOL,
        max_tokens=scope.config.max_tokens, validate=validate, attempts=2,
    )
    if data is None:
        raise LLMError("HERMES did not return a valid CC digest")
    return kept
