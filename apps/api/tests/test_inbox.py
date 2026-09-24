"""Inbox engine (docs/INBOX.md §2): scans, follow-ups, staleness, drafts, export, scheduler, privacy.

A FakeSource implements the MailSource protocol; the LLM is FakeLLM (api) or FakeClaudeSDK (subscription).
"""

from __future__ import annotations

import asyncio
import email
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from email import policy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.core import paths
from atlas.core.db import EventLog
from atlas.core.events import EventBus
from atlas.core.models import EmailDraft, WorldState
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore, fold
from atlas.inbox.drafts import build_eml
from atlas.inbox.engine import InboxEngine, NoSourceError, ScanBusyError, is_verbatim, similar
from atlas.inbox.scheduler import InboxScheduler, parse_schedule
from atlas.inbox.sources.base import MailMessage, MailSource, MailSourceError, SourceStatus
from atlas.inbox.state import business_days_between, user_tz
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live.llm import call_text, tool_use
from atlas.live.sdk import FakeClaudeSDK, call, say

SECRET = "ZETA-SECRET-4711"
TZ = user_tz()
NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)  # Thursday 10:00 in Mexico City
MODELS = ModelConfig(orchestrator="claude-opus-test", default="claude-sonnet-test", fast="claude-haiku-test")
SUB_MODELS = ModelConfig(orchestrator="opus", default="sonnet", fast="haiku")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def msg(mid: str, *, sender: str, to: list[str], subject: str, at: datetime, conv: str, me: bool = False,
        body: str = "") -> MailMessage:
    return MailMessage(
        id=mid, internet_message_id=f"<{mid}@mail.example>", conversation_id=conv, subject=subject, sender=sender,
        to=to, cc=[], received_at=at, is_from_me=me, web_link=f"https://outlook.office.com/mail/{mid}",
        preview=" ".join(body.split())[:255],
    )


class FakeSource:
    name = "fake"

    def __init__(self, items: list[tuple[MailMessage, str]], *, can_draft: bool = False, unreadable=()):
        self.messages = {m.id: m for m, _ in items}
        self.bodies = {m.id: b for m, b in items}
        self.can_draft = can_draft
        self.unreadable = set(unreadable)
        self.body_calls: list[str] = []
        self.outlook: list[EmailDraft] = []

    def add(self, m: MailMessage, body: str) -> None:
        self.messages[m.id] = m
        self.bodies[m.id] = body

    async def status(self) -> SourceStatus:
        return SourceStatus(name="fake", connected=True, account="juan@corp.mx", can_draft=self.can_draft)

    async def list_messages(self, since: datetime, limit: int = 200) -> list[MailMessage]:
        out = [m for m in self.messages.values() if m.received_at >= since]
        return sorted(out, key=lambda m: m.received_at, reverse=True)[:limit]

    async def get_body(self, message_id: str) -> str:
        self.body_calls.append(message_id)
        if message_id in self.unreadable:
            raise MailSourceError("the message could not be downloaded")
        return self.bodies[message_id]

    async def create_outlook_draft(self, draft: EmailDraft) -> str | None:
        if not self.can_draft:
            return None
        self.outlook.append(draft)
        return f"https://outlook.office.com/mail/drafts/{draft.id}"


ME = "Juan Páez <juan@corp.mx>"
ANA = "Ana Ruiz <ana@proveedor.mx>"
LUIS = "Luis Gómez <luis@banco.mx>"
CARLA = "Carla Núñez <carla@legal.mx>"


def mailbox() -> list[tuple[MailMessage, str]]:
    return [
        (msg("m1", sender=ANA, to=[ME], subject="Presupuesto obra Polanco", conv="c1",
             at=datetime(2026, 9, 21, 17, 0, tzinfo=UTC)),
         "Hola Juan,\nLe envío el presupuesto actualizado el martes sin falta.\nSaludos, Ana"),
        (msg("m2", sender=ME, to=[LUIS], subject="Crédito puente - dudas", conv="c2", me=True,
             at=datetime(2026, 9, 21, 17, 30, tzinfo=UTC)),
         "Luis, ¿me confirmas la tasa del crédito puente?\nGracias"),
        (msg("m3", sender=CARLA, to=[ME], subject="Contrato arrendamiento", conv="c3",
             at=datetime(2026, 9, 23, 18, 0, tzinfo=UTC),
             body=f"¿Puedes revisar el contrato para el viernes? Clave interna: {SECRET}"),
         f"¿Puedes revisar el contrato para el viernes? Clave interna: {SECRET} (no compartir)."),
        (msg("m4", sender=ME, to=["Equipo <equipo@corp.mx>"], subject="Reporte semanal", conv="c4", me=True,
             at=datetime(2026, 9, 23, 20, 0, tzinfo=UTC)),
         "Les mando el reporte mañana."),
        (msg("m5", sender="Ofertas <news@noreply.example>", to=[ME], subject="Ofertas de la semana", conv="c5",
             at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC)),
         "Descuentos en todo. No responda a este correo."),
    ]


EXTRACT_ITEMS = {"items": [
    {"message_id": "m1", "kind": "THEIR_COMMITMENT", "title": "Ana envía el presupuesto actualizado",
     "counterpart": ANA, "due": "2026-09-22", "priority": "HIGH",
     "excerpt": "Le envío el presupuesto actualizado el martes sin falta."},
    {"message_id": "m2", "kind": "AWAITING_REPLY", "title": "Luis confirma la tasa del crédito puente",
     "counterpart": LUIS, "excerpt": "¿me confirmas la tasa del crédito puente?"},
    {"message_id": "m3", "kind": "REQUEST_TO_ME", "title": "Revisar contrato de arrendamiento",
     "detail": "Carla pide revisar el contrato.", "counterpart": CARLA, "due": "2026-09-25",
     "excerpt": "¿Puedes revisar el contrato para el viernes?"},
    {"message_id": "m4", "kind": "MY_COMMITMENT", "title": "Enviar el reporte semanal al equipo",
     "counterpart": "Equipo <equipo@corp.mx>", "due": "2026-09-24", "excerpt": "Les mando el reporte mañana."},
]}


def draft_input(text: str) -> dict:
    fid = re.search(r"followup_id: (fup_\w+)", text).group(1)
    to = re.search(r"Suggested recipients: (.+)", text).group(1)
    subject = re.search(r"Subject: (.+)", text).group(1)
    return {"followup_id": fid, "to": [to], "subject": f"Re: {subject}",
            "body": "Hola, ¿cómo vas con esto? Quedo atento.\nSaludos,\nJuan"}


def stage(name: str):
    return lambda kw: f"STAGE: {name}" in call_text(kw)


def api_llm(extract: list | None = None) -> FakeLLM:
    llm = FakeLLM()
    for items in (extract or [EXTRACT_ITEMS]):
        llm.when(stage("INBOX EXTRACT"), tool_use("record_followups", items))
    llm.when(stage("INBOX DRAFT"), lambda kw: tool_use("draft_email", draft_input(call_text(kw))), repeat=True)
    return llm


def make_engine(registry: AgentRegistry, source, *, llm=None, sdk=None, clock=None, db: Path | None = None
                ) -> tuple[WorldStore, LiveEngine, InboxEngine]:
    store = WorldStore(registry, EventBus(log=EventLog(db) if db else None))
    common = {"context": NodeContext(registry, local_dir=Path("/nonexistent-atlas-local")), "prices": PriceTable()}
    if sdk is not None:
        live = LiveEngine(store, loader=AgentLoader(registry, SUB_MODELS), config=LiveConfig(models=SUB_MODELS),
                          backend="subscription", sdk_query=sdk, **common)
    else:
        live = LiveEngine(store, loader=AgentLoader(registry, MODELS), config=LiveConfig(models=MODELS), llm=llm,
                          **common)
    inbox = InboxEngine(store, live, source, clock=clock or (lambda: NOW))
    return store, live, inbox


async def scan(inbox: InboxEngine) -> str:
    mission = await inbox.start_scan()
    await asyncio.wait_for(inbox.wait(), 5)
    return mission.id


def everything_on_disk(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_helpers():
    assert business_days_between(date(2026, 9, 21), date(2026, 9, 24)) == 3  # Mon -> Thu
    assert business_days_between(date(2026, 9, 25), date(2026, 9, 28)) == 1  # Fri -> Mon
    assert business_days_between(date(2026, 9, 24), date(2026, 9, 24)) == 0
    assert is_verbatim("le envío  el presupuesto", "Hola.\nLe envío el\npresupuesto el martes")
    assert not is_verbatim("te mando el presupuesto", "Le envío el presupuesto")
    assert similar("Revisar contrato de arrendamiento", "Revisar el contrato de arrendamiento")
    assert not similar("Enviar reporte semanal", "Revisar contrato de arrendamiento")
    assert isinstance(FakeSource([]), MailSource)
    assert datetime(2026, 9, 24, 16, tzinfo=UTC).astimezone(TZ).hour == 10


def test_build_eml_headers():
    d = EmailDraft(to=["Luis Gómez <luis@banco.mx>"], cc=["ana@proveedor.mx"], subject="Re: Crédito puente – dudas",
                   body="Hola Luis,\n¿Pudiste revisar la tasa? Gracias.\nJuan")
    raw = build_eml(d, in_reply_to="<m2@mail.example>")
    assert b"\r\n" in raw
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["X-Unsent"] == "1"
    assert parsed["Subject"] == "Re: Crédito puente – dudas"
    assert parsed["To"].addresses[0].addr_spec == "luis@banco.mx"
    assert parsed["To"].addresses[0].display_name == "Luis Gómez"
    assert parsed["Cc"] == "ana@proveedor.mx"
    assert parsed["In-Reply-To"] == "<m2@mail.example>" and parsed["References"] == "<m2@mail.example>"
    assert parsed.get_content_charset() == "utf-8"
    assert "¿Pudiste revisar la tasa?" in parsed.get_content()
    assert "From" not in parsed  # Outlook fills in the account
    no_parent = email.message_from_bytes(build_eml(d, in_reply_to="AAMkAGraphId=="), policy=policy.default)
    assert "In-Reply-To" not in no_parent  # a Graph id is not an RFC 5322 message id


# ---------------------------------------------------------------------------
# end to end: api backend
# ---------------------------------------------------------------------------


def test_scan_end_to_end_api(registry, tmp_path):
    local = paths.local_dir()
    source = FakeSource(mailbox())
    llm = api_llm()
    clock = {"now": NOW}

    async def go():
        store, _live, inbox = make_engine(registry, source, llm=llm, clock=lambda: clock["now"],
                                         db=local / "atlas.db")
        initial = store.snapshot()
        mid = await scan(inbox)
        return store, None, inbox, initial, mid

    store, _live, inbox, initial, mid = asyncio.run(go())
    snap = store.snapshot()

    # mission: phases, tasks, report
    mission = next(m for m in snap.missions if m.id == mid)
    assert mission.objective == "Inbox scan · 2026-09-24 10:00" and mission.phase == "CLOSED"
    assert mission.mode == "live" and mission.node == "corporate"
    phases = [e.payload["mission"]["phase"] for e in store.bus.history(mission_id=mid)
              if e.type in ("mission.phase_changed", "mission.closed")]
    assert phases == ["DECOMPOSITION", "DELEGATION", "EXECUTION", "VALIDATION", "CONSOLIDATION", "REPORTING",
                      "FOLLOW_UP", "CLOSED"]
    tasks = {t.assigned_to: t for t in snap.tasks if t.mission_id == mid}
    assert tasks["hermes"].status == "COMPLETED" and tasks["alfred"].status == "COMPLETED"
    report = next(r for r in snap.mission_reports if r.mission_id == mid)
    assert report.objective_status == "ACHIEVED"
    assert "Read 5 email(s)" in report.executive_summary and "4 new follow-up(s)" in report.executive_summary
    assert "2 now waiting" in report.executive_summary and "2 draft(s) proposed" in report.executive_summary
    assert any("to review" in x for x in report.needs_human_attention)
    assert {s.agent_id: s.status for s in snap.agent_states}["hermes"] == "IDLE"

    # follow-ups of each kind
    fups = {f.kind: f for f in snap.followups}
    assert set(fups) == {"MY_COMMITMENT", "THEIR_COMMITMENT", "AWAITING_REPLY", "REQUEST_TO_ME"}
    assert fups["THEIR_COMMITMENT"].status == "WAITING" and fups["THEIR_COMMITMENT"].due == date(2026, 9, 22)
    assert fups["AWAITING_REPLY"].status == "WAITING"
    assert fups["REQUEST_TO_ME"].status == "OPEN" and fups["MY_COMMITMENT"].status == "OPEN"
    req = fups["REQUEST_TO_ME"]
    assert req.source.message_id == "m3" and req.source.sender == CARLA and req.mission_id == mid
    assert req.source.excerpt == "¿Puedes revisar el contrato para el viernes?"
    assert req.source.web_link.endswith("/m3") and req.priority == "MEDIUM"
    assert fups["AWAITING_REPLY"].counterpart == LUIS

    # drafts only for the WAITING items
    assert len(snap.drafts) == 2 and all(d.status == "PROPOSED" for d in snap.drafts)
    by_fup = {d.followup_id: d for d in snap.drafts}
    assert set(by_fup) == {fups["THEIR_COMMITMENT"].id, fups["AWAITING_REPLY"].id}
    assert fups["THEIR_COMMITMENT"].draft_id == by_fup[fups["THEIR_COMMITMENT"].id].id
    d_ana = by_fup[fups["THEIR_COMMITMENT"].id]
    assert d_ana.to == [ANA] and d_ana.subject == "Re: Presupuesto obra Polanco" and d_ana.in_reply_to == "m1"

    # evidence: email_read with subject + sender only; draft_created
    reads = [e for e in snap.evidence if e.kind == "email_read"]
    assert len(reads) == 5 and all(e.agent_id == "hermes" and e.ok for e in reads)
    assert {e.ref for e in reads} == {m.subject for m, _ in mailbox()}
    assert all(e.detail.startswith("from ") for e in reads)
    assert len([e for e in snap.evidence if e.kind == "draft_created" and e.agent_id == "alfred"]) == 2
    summaries = [e.summary for e in store.bus.history()]
    assert any(s.startswith("New follow-up (request to me) · 'Revisar contrato de arrendamiento' · Carla Núñez")
               for s in summaries)
    assert any(s.startswith("Follow-up overdue · 'Ana envía el presupuesto actualizado'") for s in summaries)
    assert any(s.startswith("Draft proposed · 'Re: Presupuesto obra Polanco' → Ana Ruiz") for s in summaries)
    assert any(s.startswith("HERMES read 'Contrato arrendamiento' · Carla Núñez") for s in summaries)

    # the reducer reproduces the state (followups / drafts included)
    folded = fold(initial, store.bus.history())
    assert folded.followups == snap.followups and folded.drafts == snap.drafts

    # one extraction call (5 messages ≤ batch of 10), HERMES' prompt and tool
    extract_calls = [c for c in llm.calls if stage("INBOX EXTRACT")(c)]
    assert len(extract_calls) == 1
    assert extract_calls[0]["tool_choice"] == {"type": "tool", "name": "record_followups"}
    assert "You are HERMES" in call_text(extract_calls[0]) and "SENT BY ME" in call_text(extract_calls[0])

    # privacy: the secret in m3's body (and preview) is nowhere: event log, db, state file, any file
    assert SECRET in call_text(extract_calls[0])  # it did reach the model (in memory only)
    assert SECRET not in store.snapshot().model_dump_json()
    assert all(SECRET not in e.model_dump_json() for e in store.bus.history())
    assert SECRET.encode() not in everything_on_disk(local)
    state_json = (local / "inbox" / "state.json").read_text(encoding="utf-8")
    assert "Contrato" not in state_json and "Carla" not in state_json and '"m3"' in state_json

    # second scan, same mailbox: nothing re-read, nothing duplicated
    async def rescan():
        clock["now"] = NOW + timedelta(hours=5)
        n_body = len(source.body_calls)
        mid2 = await scan(inbox)
        return mid2, n_body

    mid2, n_body = asyncio.run(rescan())
    snap = store.snapshot()
    assert len([c for c in llm.calls if stage("INBOX EXTRACT")(c)]) == 1
    assert source.body_calls[n_body:] == []  # no body fetched (no new mail, no new drafts)
    assert len(snap.followups) == 4 and len(snap.drafts) == 2
    report2 = next(r for r in snap.mission_reports if r.mission_id == mid2)
    assert "0 new follow-up(s)" in report2.executive_summary and report2.objective_status == "ACHIEVED"

    # a newer email in Carla's conversation updates the open follow-up instead of adding one
    source.add(msg("m6", sender=CARLA, to=[ME], subject="RE: Contrato arrendamiento", conv="c3",
                   at=NOW + timedelta(hours=6)),
               "Te recuerdo revisar el contrato de arrendamiento para el viernes, por favor.")
    llm.when(stage("INBOX EXTRACT"), tool_use("record_followups", {"items": [
        {"message_id": "m6", "kind": "REQUEST_TO_ME", "title": "Revisar el contrato de arrendamiento",
         "counterpart": CARLA, "due": "2026-09-25", "priority": "HIGH",
         "excerpt": "Te recuerdo revisar el contrato de arrendamiento para el viernes, por favor."}]}))

    async def third():
        clock["now"] = NOW + timedelta(hours=7)
        return await scan(inbox)

    mid3 = asyncio.run(third())
    snap = store.snapshot()
    assert len(snap.followups) == 4
    req2 = next(f for f in snap.followups if f.kind == "REQUEST_TO_ME")
    assert req2.id == req.id and req2.source.message_id == "m6" and req2.priority == "HIGH"
    report3 = next(r for r in snap.mission_reports if r.mission_id == mid3)
    assert "1 updated" in report3.executive_summary


def test_draft_decision_eml_and_outlook(registry):
    source = FakeSource(mailbox())

    async def go():
        store, _live, inbox = make_engine(registry, source, llm=api_llm())
        await scan(inbox)
        drafts = store.drafts()
        ana = next(d for d in drafts if d.in_reply_to == "m1")
        luis = next(d for d in drafts if d.in_reply_to == "m2")
        approved = await inbox.decide_draft(ana.id, "APPROVED", body="Hola Ana,\n¿Tienes ya el presupuesto?\nJuan",
                                            cc=["compras@corp.mx"])
        with pytest.raises(Exception, match="already exported"):
            await inbox.decide_draft(ana.id, "APPROVED")
        discarded = await inbox.decide_draft(luis.id, "DISCARDED")
        # Outlook-capable source: the approved draft lands in Outlook Drafts
        source.can_draft = True
        one_shot = await inbox.draft_now(next(f.id for f in store.followups() if f.kind == "REQUEST_TO_ME"))
        outlook = await inbox.decide_draft(one_shot.id, "APPROVED")
        return store, approved, discarded, one_shot, outlook

    store, approved, discarded, one_shot, outlook = asyncio.run(go())
    assert approved.status == "EXPORTED" and approved.export == "eml"
    assert approved.download_url == f"/drafts/{approved.id}/eml"
    path = paths.local_dir() / "outputs" / "corporate" / "inbox" / f"{approved.id}.eml"
    parsed = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    assert parsed["X-Unsent"] == "1" and parsed["Subject"] == "Re: Presupuesto obra Polanco"
    assert parsed["To"].addresses[0].addr_spec == "ana@proveedor.mx" and parsed["Cc"] == "compras@corp.mx"
    assert parsed["In-Reply-To"] == "<m1@mail.example>"  # the internet message id, from the scan state
    assert "¿Tienes ya el presupuesto?" in parsed.get_content()
    assert discarded.status == "DISCARDED"
    assert one_shot.status == "PROPOSED" and one_shot.to == [CARLA]
    assert outlook.export == "outlook_drafts" and outlook.status == "EXPORTED"
    assert outlook.download_url == f"https://outlook.office.com/mail/drafts/{outlook.id}"
    assert source.outlook and source.outlook[0].id == outlook.id
    assert not (paths.local_dir() / "outputs" / "corporate" / "inbox" / f"{outlook.id}.eml").exists()
    summaries = [e.summary for e in store.bus.history()]
    assert "Draft exported as .eml · 'Re: Presupuesto obra Polanco'" in summaries
    assert "Draft discarded · 'Re: Crédito puente - dudas'" in summaries
    assert any(s.startswith("Draft saved to Outlook Drafts · ") for s in summaries)


def test_unreadable_message_is_reported_and_retried(registry):
    source = FakeSource(mailbox()[:2], unreadable={"m2"})
    items = {"items": [EXTRACT_ITEMS["items"][0]]}
    llm = api_llm([items, {"items": [EXTRACT_ITEMS["items"][1]]}])
    clock = {"now": NOW}

    async def go():
        store, _live, inbox = make_engine(registry, source, llm=llm, clock=lambda: clock["now"])
        mid = await scan(inbox)
        first = store.snapshot()
        source.unreadable.clear()
        clock["now"] = NOW + timedelta(hours=1)
        await scan(inbox)
        return store, mid, first, inbox

    store, mid, first, inbox = asyncio.run(go())
    report = next(r for r in first.mission_reports if r.mission_id == mid)
    assert report.objective_status == "PARTIAL"
    assert any("Unreadable email: 'Crédito puente - dudas'" in x for x in report.needs_human_attention)
    bad = [e for e in first.evidence if e.kind == "email_read" and not e.ok]
    assert len(bad) == 1 and bad[0].ref == "Crédito puente - dudas"
    assert inbox.state.is_processed("m2") and len(store.followups(kind="AWAITING_REPLY")) == 1  # retried


def test_scan_guards(registry):
    async def go():
        store, _live, inbox = make_engine(registry, None, llm=api_llm())
        with pytest.raises(NoSourceError) as exc:
            await inbox.start_scan()
        assert "ATLAS_MAIL_FOLDER" in exc.value.hint
        inbox.source = FakeSource(mailbox())
        await inbox.start_scan()
        with pytest.raises(ScanBusyError):
            await inbox.start_scan()
        await asyncio.wait_for(inbox.wait(), 5)
        # cancelling the scan mission from outside stops it cleanly
        slow = asyncio.Event()

        class Slow(FakeSource):
            async def list_messages(self, since, limit=200):
                await slow.wait()
                return await super().list_messages(since, limit)

        inbox.source = Slow(mailbox())
        inbox.state.last_scan = None
        mission = await inbox.start_scan()
        await asyncio.sleep(0.01)
        await store.cancel_mission(mission.id)
        slow.set()
        await asyncio.wait_for(inbox.wait(), 5)
        return store, mission.id

    store, mid = asyncio.run(go())
    assert store.mission(mid).phase == "CLOSED" and not store.tasks_for(mid)


# ---------------------------------------------------------------------------
# subscription backend
# ---------------------------------------------------------------------------


def test_scan_end_to_end_subscription(registry):
    local = paths.local_dir()
    sdk = FakeClaudeSDK()
    sdk.when(lambda s: "STAGE: INBOX EXTRACT" in s.prompt, [call("record_followups", EXTRACT_ITEMS), say("done")])
    sdk.when(lambda s: "STAGE: INBOX DRAFT" in s.prompt,
             [lambda s: call("draft_email", draft_input(s.prompt)), say("done")], repeat=True)
    source = FakeSource(mailbox())

    async def go():
        store, _live, inbox = make_engine(registry, source, sdk=sdk, db=local / "atlas.db")
        mid = await scan(inbox)
        d = store.drafts()[0]
        approved = await inbox.decide_draft(d.id, "APPROVED")
        return store, mid, approved

    store, mid, approved = asyncio.run(go())
    snap = store.snapshot()
    assert next(r for r in snap.mission_reports if r.mission_id == mid).objective_status == "ACHIEVED"
    assert len(snap.followups) == 4 and len(snap.drafts) == 2
    assert {f.kind for f in snap.followups if f.status == "WAITING"} == {"THEIR_COMMITMENT", "AWAITING_REPLY"}
    extract = sdk.matching(lambda s: "STAGE: INBOX EXTRACT" in s.prompt)[0]
    assert "mcp__atlas__record_followups" in extract.options.allowed_tools and "You are HERMES" in extract.system
    assert extract.results == [("record_followups", "Received. Your work is done: stop now.", False)]
    assert next(m for m in snap.missions if m.id == mid).usage.llm_calls >= 3
    assert approved.export == "eml" and approved.status == "EXPORTED"
    assert SECRET not in snap.model_dump_json()
    assert SECRET.encode() not in everything_on_disk(local)


# ---------------------------------------------------------------------------
# scheduler (injected clock)
# ---------------------------------------------------------------------------


class FakeScanEngine:
    def __init__(self, clock, last_scan=None, source=True):
        self.tz = TZ
        self.clock = clock
        self.last_scan = last_scan
        self.source = object() if source else None
        self.scheduler = None
        self.calls: list[tuple[datetime, str]] = []

    def now(self):
        return self.clock()

    async def start_scan(self, trigger="manual"):
        if self.source is None:
            raise NoSourceError("No mail source is configured", "set ATLAS_MAIL_FOLDER")
        self.calls.append((self.clock().astimezone(TZ), trigger))
        self.last_scan = self.clock()


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_schedule_parsing_and_slots():
    assert parse_schedule(None) == parse_schedule("08:00,15:00") and len(parse_schedule(None)) == 2
    assert parse_schedule("") == [] and parse_schedule("  ") == []
    assert [t.strftime("%H:%M") for t in parse_schedule("15:00, 8:30, bad, 8:30")] == ["08:30", "15:00"]
    sched = InboxScheduler(FakeScanEngine(lambda: NOW), parse_schedule("08:00,15:00"))
    assert sched.next_slot(local(2026, 9, 25, 16)) == local(2026, 9, 28, 8)  # Fri after 15:00 -> Mon 08:00
    assert sched.next_slot(local(2026, 9, 24, 8)) == local(2026, 9, 24, 15)  # strictly after
    assert sched.previous_slot(local(2026, 9, 26, 10)) == local(2026, 9, 25, 15)  # Saturday -> Fri 15:00
    assert sched.previous_slot(local(2026, 9, 28, 8)) == local(2026, 9, 28, 8)
    weekend = InboxScheduler(FakeScanEngine(lambda: NOW), parse_schedule("08:00"), weekdays_only=False)
    assert weekend.next_slot(local(2026, 9, 25, 9)) == local(2026, 9, 26, 8)


def _run_scheduler(engine, times, clock_box, *, until_calls: int, max_sleeps: int = 200):
    sleeps = {"n": 0}

    async def fake_sleep(seconds):
        sleeps["n"] += 1
        clock_box["now"] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def go():
        sched = InboxScheduler(engine, times, clock=lambda: clock_box["now"], sleep=fake_sleep, poll=3600)
        await sched.start()
        while len(engine.calls) < until_calls and sleeps["n"] < max_sleeps:
            await asyncio.sleep(0)
        await sched.stop()
        return sched

    return asyncio.run(go())


def test_scheduler_catch_up_then_slots():
    box = {"now": local(2026, 9, 28, 7)}  # Monday 07:00; last scan Friday 09:00 < Friday 15:00 slot
    engine = FakeScanEngine(lambda: box["now"], last_scan=local(2026, 9, 25, 9))
    _run_scheduler(engine, parse_schedule("08:00,15:00"), box, until_calls=4)
    assert engine.calls == [
        (local(2026, 9, 28, 7), "catch-up"),
        (local(2026, 9, 28, 8), "scheduled"),
        (local(2026, 9, 28, 15), "scheduled"),
        (local(2026, 9, 29, 8), "scheduled"),
    ]


def test_scheduler_no_catch_up_when_recent():
    box = {"now": local(2026, 9, 26, 10)}  # Saturday; last scan Friday 16:00 is after the Friday 15:00 slot
    engine = FakeScanEngine(lambda: box["now"], last_scan=local(2026, 9, 25, 16))
    _run_scheduler(engine, parse_schedule("08:00,15:00"), box, until_calls=1)
    assert engine.calls == [(local(2026, 9, 28, 8), "scheduled")]  # weekend skipped


def test_scheduler_without_source_logs_once(caplog):
    box = {"now": local(2026, 9, 28, 7)}
    engine = FakeScanEngine(lambda: box["now"], source=False)
    with caplog.at_level(logging.INFO, logger="atlas.inbox"):
        _run_scheduler(engine, parse_schedule("08:00,15:00"), box, until_calls=99, max_sleeps=12)
    assert engine.calls == []
    assert len([r for r in caplog.records if "idle" in r.getMessage()]) == 1


def test_scheduler_disabled_and_fast_stop():
    async def go():
        engine = FakeScanEngine(lambda: datetime.now(UTC), last_scan=datetime.now(UTC))
        off = InboxScheduler(engine, [])
        await off.start()
        assert not off.running and off.next_scan() is None
        on = InboxScheduler(engine, parse_schedule("08:00,15:00"), weekdays_only=False)
        await on.start()
        await asyncio.sleep(0.01)
        assert on.running and on.next_scan() is not None
        t0 = time.monotonic()
        await on.stop()
        return time.monotonic() - t0, on

    elapsed, on = asyncio.run(go())
    assert elapsed < 0.5 and not on.running


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _poll(client: TestClient, pred, timeout: float = 5.0) -> WorldState:
    deadline = time.monotonic() + timeout
    while True:
        state = WorldState.model_validate(client.get("/state").json())
        if pred(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_http_inbox(monkeypatch):
    from atlas.main import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for k in ("ATLAS_MAIL_SOURCE", "ATLAS_MS_CLIENT_ID", "ATLAS_MAIL_FOLDER", "ATLAS_INBOX_SCHEDULE"):
        monkeypatch.delenv(k, raising=False)
    with TestClient(app) as client:
        st = client.get("/inbox/status").json()
        assert st["source"] is None and st["connected"] is False and "ATLAS_MAIL_FOLDER" in st["hint"]
        assert st["schedule"] == ["08:00", "15:00"] and st["next_scan"] is not None and st["last_scan"] is None
        r = client.post("/inbox/scan")
        assert r.status_code == 409 and "ATLAS_MS_CLIENT_ID" in r.json()["detail"]
        assert client.post("/inbox/connect").status_code == 409

        inbox = app.state.inbox
        inbox.source = FakeSource(mailbox())
        inbox.clock = lambda: NOW
        r = client.post("/inbox/scan")
        assert r.status_code == 422  # no LLM backend
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        app.state.live.llm = api_llm()
        r = client.post("/inbox/scan")
        assert r.status_code == 200 and r.json()["objective"].startswith("Inbox scan · ")
        mid = r.json()["id"]
        _poll(client, lambda s: next(m for m in s.missions if m.id == mid).phase == "CLOSED")

        st = client.get("/inbox/status").json()
        assert st["source"] == "fake" and st["connected"] and st["processed_count"] == 5 and st["last_scan"]
        assert st["scanning"] is False and st["account"] == "juan@corp.mx"
        fups = client.get("/followups").json()
        assert len(fups) == 4 and fups[0]["status"] == "WAITING"
        assert len(client.get("/followups", params={"status": "WAITING"}).json()) == 2
        mine = client.get("/followups", params={"kind": "MY_COMMITMENT"}).json()[0]
        r = client.patch(f"/followups/{mine['id']}", json={"due": "2026-10-01", "priority": "HIGH"})
        assert r.status_code == 200 and r.json()["due"] == "2026-10-01" and r.json()["priority"] == "HIGH"
        r = client.patch(f"/followups/{mine['id']}", json={"due": None, "status": "DONE"})
        assert r.json()["due"] is None and r.json()["status"] == "DONE"
        assert client.patch(f"/followups/{mine['id']}", json={"status": "NOPE"}).status_code == 422
        assert client.patch("/followups/fup_nope", json={"status": "DONE"}).status_code == 404
        assert client.post(f"/followups/{mine['id']}/draft").status_code == 409  # done

        drafts = client.get("/drafts", params={"status": "PROPOSED"}).json()
        assert len(drafts) == 2
        d = drafts[0]
        r = client.post(f"/drafts/{d['id']}/decision", json={"decision": "APPROVED", "subject": "Seguimiento"})
        assert r.status_code == 200
        out = r.json()
        assert out["status"] == "EXPORTED" and out["export"] == "eml" and out["subject"] == "Seguimiento"
        eml = client.get(out["download_url"])
        assert eml.status_code == 200 and eml.headers["content-type"].startswith("message/rfc822")
        assert b"X-Unsent: 1" in eml.content and "Seguimiento.eml" in eml.headers["content-disposition"]
        assert client.post(f"/drafts/{d['id']}/decision", json={"decision": "DISCARDED"}).status_code == 409
        assert client.get(f"/drafts/{drafts[1]['id']}/eml").status_code == 404
        assert client.post(f"/drafts/{drafts[1]['id']}/decision", json={"decision": "SENT"}).status_code == 422

        req = client.get("/followups", params={"kind": "REQUEST_TO_ME"}).json()[0]
        r = client.post(f"/followups/{req['id']}/draft")
        assert r.status_code == 200 and r.json()["status"] == "PROPOSED" and r.json()["followup_id"] == req["id"]
        assert client.get("/followups", params={"kind": "REQUEST_TO_ME"}).json()[0]["draft_id"] == r.json()["id"]

        events = client.get("/events").json()
        assert any(e["type"] == "followup.upserted" for e in events)
        assert any(e["type"] == "draft.upserted" for e in events)
        assert all(SECRET not in str(e) for e in events)
