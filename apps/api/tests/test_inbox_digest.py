"""CC digest (docs/INBOX.md §4): selection, rules, grouping, summaries, figures, asks_me, privacy, API."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_inbox import (
    NOW,
    FakeSource,
    _poll,
    everything_on_disk,
    make_engine,
    msg,
    scan,
    stage,
)

from atlas.core import paths
from atlas.core.registry import AgentRegistry
from atlas.inbox.digest import (
    DigestRules,
    classify,
    figure_ok,
    is_automated,
    load_rules,
    merge_headlines,
    normalize_subject,
    rules_path,
    thread_key,
)
from atlas.inbox.engine import ScanBusyError
from atlas.live import FakeLLM
from atlas.live.llm import call_text, tool_use
from atlas.live.sdk import FakeClaudeSDK, call, say

SECRET = "DIGEST-SECRET-9321"
ME = "Juan Páez <juan@corp.mx>"  # the signed-in account (FakeSource.status)
ALIAS = "jpaez@corp.mx"  # ATLAS_MAIL_ME
ANA = "Ana Ruiz <ana@constructora.mx>"
PEDRO = "Pedro Díaz <pedro@constructora.mx>"
LUIS = "Luis Gómez <luis@banco.mx>"
T0 = NOW - timedelta(hours=20)

RULES = """\
include_to_from: [consejero@board.mx]
exclude_senders: [vendor.io]
exclude_keywords: [comida]
projects:
  Polanco: [polanco, torre p]
  Bogus: not-a-list
max_threads: 10
"""


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def cc_mailbox() -> list:
    m1 = msg("d1", sender=PEDRO, to=[ANA], subject="Avance obra Polanco", conv="c-pol", at=at(0))
    m1.cc = [ME]
    m1.preview = ""  # the secret lives only in the body
    m2 = msg("d2", sender=ANA, to=[PEDRO], subject="RE: Avance obra Polanco", conv="c-pol", at=at(30))
    m2.cc = [ME, "Otro <otro@corp.mx>"]
    m3 = msg("d3", sender=LUIS, to=["Comité <comite@banco.mx>"], subject="Comité de crédito", conv="", at=at(40))
    m3.cc = [ALIAS]
    m4 = msg("d4", sender=LUIS, to=["Comité <comite@banco.mx>"], subject="RV: Comité de crédito", conv="",
             at=at(50))
    m4.cc = [ALIAS]
    m5 = msg("d5", sender="Carla <carla@legal.mx>", to=[ME], subject="Contrato", conv="c5", at=at(60))
    m6 = msg("d6", sender=ME, to=[ANA], subject="Mi correo", conv="c6", at=at(70), me=True)
    m6.cc = [ALIAS]
    m7 = msg("d7", sender="SharePoint <notifications@sharepoint.com>", to=["x@corp.mx"], subject="Doc updated",
             conv="c7", at=at(80))
    m7.cc = [ME]
    m8 = msg("d8", sender="Beto <beto@corp.mx>", to=["y@corp.mx"], subject="Accepted: Junta semanal", conv="c8",
             at=at(90))
    m8.cc = [ME]
    m9 = msg("d9", sender="Consejero <consejero@board.mx>", to=[ME], subject="Weekly board update", conv="c9",
             at=at(100))
    m10 = msg("d10", sender="Spam <sales@vendor.io>", to=["z@corp.mx"], subject="Oferta", conv="c10", at=at(110))
    m10.cc = [ME]
    m11 = msg("d11", sender="Rosa <rosa@corp.mx>", to=["w@corp.mx"], subject="Comida del viernes", conv="c11",
              at=at(120))
    m11.cc = [ME]
    return [
        (m1, (f"El avance de la torre Polanco es 62%. Presupuesto ejercido: $1,250,000.\n"
              f"Juan, ¿puedes aprobar el cambio de proveedor? Clave de acceso: {SECRET}")),
        (m2, "Confirmo, arrancamos el lunes 29."),
        (m3, "Se aprobó la línea por 30 millones."),
        (m4, "Reenvío el acta del comité."),
        (m5, "Revisa el contrato por favor."),
        (m6, "Mi propio correo."),
        (m7, "A document was updated."),
        (m8, "Beto accepted."),
        (m9, "Revenue grew 12% this quarter."),
        (m10, "Descuentos."),
        (m11, "¿Vamos a la comida?"),
    ]


def summarize(text: str) -> dict:
    convs = re.findall(r"##### conversation_id: (.+)", text)
    threads = []
    for c in convs:
        if c == "c-pol":
            threads.append({"conversation_id": c, "summary": ["La torre va al 62%", "Arranque el lunes 29"],
                            "decisions": ["Arrancar el lunes 29"],
                            "figures": ["Avance: 62%", "Presupuesto ejercido: $1,250,000", "Costo total: $9,999"],
                            "asks_me": "Aprobar el cambio de proveedor", "importance": "HIGH"})
        else:
            threads.append({"conversation_id": c, "summary": [f"Resumen de {c}"], "figures": ["30 millones"],
                            "importance": "LOW"})
    threads.append({"conversation_id": "made-up", "summary": ["x"], "importance": "LOW"})  # dropped on retry
    return {"headline": ["Polanco al 62%", "Línea de crédito aprobada"], "threads": threads}


def summarize_valid(text: str) -> dict:
    data = summarize(text)
    data["threads"] = [t for t in data["threads"] if t["conversation_id"] != "made-up"]
    return data


def digest_llm() -> FakeLLM:
    llm = FakeLLM()
    llm.when(stage("INBOX EXTRACT"), tool_use("record_followups", {"items": []}), repeat=True)
    llm.when(stage("INBOX DIGEST"), lambda kw: tool_use("summarize_threads", summarize(call_text(kw))),
             lambda kw: tool_use("summarize_threads", summarize_valid(call_text(kw))))
    return llm


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_ME", ALIAS)
    path = rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RULES, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# pure parts
# ---------------------------------------------------------------------------


def test_selection_rules():
    me = {"juan@corp.mx", ALIAS}
    r = DigestRules(include_to_from=["consejero@board.mx"])
    box = {m.id: m for m, _ in cc_mailbox()}
    assert classify(box["d1"], me, r) == "cc"
    assert classify(box["d3"], me, r) == "cc"  # ATLAS_MAIL_ME alias in cc
    assert classify(box["d5"], me, r) is None  # in To
    assert classify(box["d6"], me, r) is None  # I sent it
    assert classify(box["d9"], me, r) == "to_from"
    assert classify(box["d9"], me, DigestRules()) is None
    assert classify(box["d1"], set(), r) is None  # unknown addresses: nothing is CC
    assert is_automated(box["d7"]) and is_automated(box["d8"]) and not is_automated(box["d1"])
    assert normalize_subject("RE: Fwd: RV:  Comité  de crédito") == "Comité de crédito"
    assert normalize_subject("Re[2]: FW: x") == "x"
    assert thread_key(box["d3"]) == thread_key(box["d4"]) == "subject:comité de crédito"
    assert thread_key(box["d1"]) == "c-pol"
    src = "Presupuesto ejercido: $1,250,000. Avance 62%. Fecha 2025"
    assert figure_ok("Presupuesto ejercido: $1,250,000", src)
    assert figure_ok("Avance: 62%", src)  # the number is verbatim, the words are the model's
    assert not figure_ok("Costo total: $9,999", src)
    assert not figure_ok("Año 202", src)  # no partial-number matches
    assert not figure_ok("un buen avance", src)
    assert merge_headlines([["A", "B", "C"], ["D", "a"], ["E"]]) == ["A", "D", "E"]


def test_rules_file_missing_bad_and_valid(caplog):
    path = rules_path()
    assert not path.exists()
    assert load_rules() == DigestRules()
    assert path.exists() and "include_to_from" in path.read_text(encoding="utf-8")
    assert load_rules() == DigestRules()  # the example is all comments
    assert paths.is_within(path, paths.local_dir())
    path.write_text("projects: [unclosed\n  - : :", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="atlas.inbox"):
        assert load_rules() == DigestRules()
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    path.write_text(RULES, encoding="utf-8")
    r = load_rules()
    assert r.include_to_from == ["consejero@board.mx"] and r.exclude_senders == ["vendor.io"]
    assert r.projects == {"Polanco": ["polanco", "torre p"], "Bogus": ["not-a-list"]} and r.max_threads == 10


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def _check_digest(store, mid):
    snap = store.snapshot()
    assert len(snap.digests) == 1
    d = snap.digests[0]
    assert d.mission_id == mid and d.skipped == 4  # automated ×2, excluded sender, excluded keyword
    assert d.headline == ["Polanco al 62%", "Línea de crédito aprobada"]
    assert d.window_end is not None and d.window_start < d.window_end
    by_conv = {t.conversation_id: t for t in d.threads}
    assert set(by_conv) == {"c-pol", "comité de crédito", "c9"}
    pol = by_conv["c-pol"]
    assert d.threads[0].conversation_id == "c-pol"  # HIGH first
    assert pol.project == "Polanco" and pol.subject == "Avance obra Polanco" and pol.importance == "HIGH"
    assert [m.message_id for m in pol.messages] == ["d1", "d2"]
    assert all(m.excerpt == "" for t in d.threads for m in t.messages)
    assert pol.figures == ["Avance: 62%", "Presupuesto ejercido: $1,250,000"]  # $9,999 not in the emails
    assert pol.decisions == ["Arrancar el lunes 29"] and len(pol.summary) == 2
    assert ME not in pol.participants and ANA in pol.participants and PEDRO in pol.participants
    comite = by_conv["comité de crédito"]
    assert [m.message_id for m in comite.messages] == ["d3", "d4"] and comite.subject == "Comité de crédito"
    assert comite.figures == ["30 millones"] and comite.project is None and comite.asks_me is None
    assert by_conv["c9"].figures == []  # "30 millones" is not in the board update
    # asks_me -> REQUEST_TO_ME follow-up, linked
    f = next(x for x in snap.followups if x.id == pol.followup_id)
    assert f.kind == "REQUEST_TO_ME" and f.title == "Aprobar el cambio de proveedor" and f.counterpart == ANA
    assert f.source.message_id == "d2" and f.priority == "HIGH"
    ev = [e for e in store.bus.history() if e.type == "digest.ready"]
    assert len(ev) == 1 and ev[0].summary == "CC digest · 3 threads · Polanco al 62%" and ev[0].mission_id == mid
    report = next(r for r in snap.mission_reports if r.mission_id == mid)
    assert "CC digest: 3 threads" in report.executive_summary
    task = next(t for t in snap.tasks if t.mission_id == mid and t.title.startswith("CC digest"))
    assert task.assigned_to == "hermes" and task.status == "COMPLETED"
    # privacy: the secret in d1's body is nowhere
    local = paths.local_dir()
    assert SECRET not in snap.model_dump_json()
    assert all(SECRET not in e.model_dump_json() for e in store.bus.history())
    assert SECRET.encode() not in everything_on_disk(local)
    return d


def test_digest_end_to_end_api(registry, rules):
    llm = digest_llm()
    source = FakeSource(cc_mailbox())

    async def go():
        store, _live, inbox = make_engine(registry, source, llm=llm, db=paths.local_dir() / "atlas.db")
        mid = await scan(inbox)
        # an asks_me on a later scan updates the same follow-up instead of adding one
        extra = msg("d12", sender=ANA, to=[PEDRO], subject="RE: Avance obra Polanco", conv="c-pol",
                    at=NOW + timedelta(hours=2))
        extra.cc = [ME]
        source.add(extra, "Juan, recuerda aprobar el cambio de proveedor.")
        inbox.clock = lambda: NOW + timedelta(hours=3)
        llm.when(stage("INBOX DIGEST"), lambda kw: tool_use("summarize_threads", summarize_valid(call_text(kw))))
        mid2 = await scan(inbox)
        return store, mid, mid2

    store, mid, mid2 = asyncio.run(go())
    digest_calls = [c for c in llm.calls if stage("INBOX DIGEST")(c)]
    assert len(digest_calls) == 3  # scan 1: first answer had an unknown id (retried); scan 2: one call
    assert "You are HERMES" in call_text(digest_calls[0]) and SECRET in call_text(digest_calls[0])
    assert digest_calls[0]["tool_choice"] == {"type": "tool", "name": "summarize_threads"}
    first = store.snapshot().digests
    d1 = next(d for d in first if d.mission_id == mid)
    asks = store.followups(kind="REQUEST_TO_ME")
    assert len(asks) == 1 and asks[0].source.message_id == "d12"
    d2 = next(d for d in first if d.mission_id == mid2)
    assert len(d2.threads) == 1 and d2.threads[0].followup_id == asks[0].id == d1.threads[0].followup_id
    assert store.digests(limit=1)[0].id == d2.id
    assert {t.conversation_id for t in d1.threads} == {"c-pol", "comité de crédito", "c9"} and d1.skipped == 4
    assert SECRET not in store.snapshot().model_dump_json()
    assert SECRET.encode() not in everything_on_disk(paths.local_dir())


def test_digest_single_scan_api(registry, rules):
    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(cc_mailbox()), llm=digest_llm(),
                                          db=paths.local_dir() / "atlas.db")
        mid = await scan(inbox)
        return store, mid

    store, mid = asyncio.run(go())
    _check_digest(store, mid)


def test_digest_end_to_end_subscription(registry, rules):
    sdk = FakeClaudeSDK()
    sdk.when(lambda s: "STAGE: INBOX EXTRACT" in s.prompt, [call("record_followups", {"items": []}), say("ok")],
             repeat=True)
    sdk.when(lambda s: "STAGE: INBOX DIGEST" in s.prompt,
             [lambda s: call("summarize_threads", summarize_valid(s.prompt)), say("done")])

    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(cc_mailbox()), sdk=sdk,
                                          db=paths.local_dir() / "atlas.db")
        mid = await scan(inbox)
        return store, mid

    store, mid = asyncio.run(go())
    _check_digest(store, mid)
    sess = sdk.matching(lambda s: "STAGE: INBOX DIGEST" in s.prompt)[0]
    assert "mcp__atlas__summarize_threads" in sess.options.allowed_tools
    assert sess.results == [("summarize_threads", "Received. Your work is done: stop now.", False)]


def test_batches_of_eight_and_include_keywords(registry, monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_ME", ALIAS)
    path = rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("include_keywords: [obra]\n", encoding="utf-8")
    items = []
    for i in range(11):
        m = msg(f"b{i}", sender=f"P{i} <p{i}@x.mx>", to=["t@x.mx"], subject=f"Tema {i}", conv=f"k{i}",
                at=at(i))
        m.cc = [ALIAS]
        items.append((m, f"Avance de obra número {i}." if i < 10 else "Nada que ver."))
    llm = FakeLLM()
    llm.when(stage("INBOX EXTRACT"), tool_use("record_followups", {"items": []}), repeat=True)
    n = {"i": 0}

    def answer(kw):
        n["i"] += 1
        convs = re.findall(r"##### conversation_id: (.+)", call_text(kw))
        return tool_use("summarize_threads", {
            "headline": [f"Lote {n['i']} A", f"Lote {n['i']} B"],
            "threads": [{"conversation_id": c, "summary": ["ok"], "importance": "MEDIUM"} for c in convs]})

    llm.when(stage("INBOX DIGEST"), answer, repeat=True)

    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(items), llm=llm)
        await scan(inbox)
        return store

    store = asyncio.run(go())
    calls = [c for c in llm.calls if stage("INBOX DIGEST")(c)]
    sizes = [len(re.findall(r"##### conversation_id:", call_text(c))) for c in calls]
    assert sizes == [8, 2]
    d = store.digests()[0]
    assert len(d.threads) == 10 and d.skipped == 1  # "Nada que ver" lacks the keyword
    assert d.headline == ["Lote 1 A", "Lote 2 A", "Lote 1 B"]


def test_no_candidates_no_digest(registry, monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_ME", ALIAS)
    box = [x for x in cc_mailbox() if x[0].id in ("d5", "d6", "d7", "d8", "d9")]  # To me, mine, automated
    llm = FakeLLM()
    llm.when(stage("INBOX EXTRACT"), tool_use("record_followups", {"items": []}), repeat=True)

    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(box), llm=llm)
        mid = await scan(inbox)
        return store, mid

    store, mid = asyncio.run(go())
    assert store.digests() == []
    assert not [e for e in store.bus.history() if e.type == "digest.ready"]
    assert not [c for c in llm.calls if stage("INBOX DIGEST")(c)]
    report = store.mission_reports_for(mid)[0]
    note = "2 CC emails, all excluded by rules/automated"  # d7 and d8 are automated
    assert f"CC digest: none · {note}" in report.executive_summary and report.objective_status == "ACHIEVED"
    assert f"CC digest · {note}" in [e.summary for e in store.bus.history(mission_id=mid)]


def test_http_digests(monkeypatch, rules):
    from atlas.main import app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with TestClient(app) as client:
        assert client.get("/digests").json() == []
        inbox = app.state.inbox
        inbox.source = FakeSource(cc_mailbox())
        inbox.clock = lambda: NOW
        app.state.live.llm = digest_llm()
        mid = client.post("/inbox/scan").json()["id"]
        deadline = time.monotonic() + 5
        while not any(m["id"] == mid and m["phase"] == "CLOSED" for m in client.get("/state").json()["missions"]):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        digests = client.get("/digests", params={"limit": 5}).json()
        assert len(digests) == 1 and len(digests[0]["threads"]) == 3
        one = client.get(f"/digests/{digests[0]['id']}").json()
        assert one["headline"][0] == "Polanco al 62%" and one["threads"][0]["followup_id"]
        assert client.get("/digests/dig_nope").status_code == 404
        assert client.get("/digests", params={"limit": 0}).status_code == 422
        events = client.get("/events").json()
        assert any(e["type"] == "digest.ready" for e in events)
        assert all(SECRET not in str(e) for e in events)


# ---------------------------------------------------------------------------
# on-demand window digest (POST /digests/run)
# ---------------------------------------------------------------------------

ASK_ITEM = {"items": [{"message_id": "d1", "kind": "REQUEST_TO_ME", "title": "Aprobar el cambio de proveedor",
                       "counterpart": PEDRO, "excerpt": "¿puedes aprobar el cambio de proveedor?"}]}


def _extract_items(text: str) -> dict:
    return ASK_ITEM if "message_id: d1 " in text else {"items": []}


def _backend(kind: str):
    """(llm, sdk, count of extraction calls, count of digest calls) for the api or subscription backend."""
    if kind == "api":
        llm = FakeLLM()
        llm.when(stage("INBOX EXTRACT"), lambda kw: tool_use("record_followups", _extract_items(call_text(kw))),
                 repeat=True)
        llm.when(stage("INBOX DIGEST"), lambda kw: tool_use("summarize_threads", summarize_valid(call_text(kw))),
                 repeat=True)
        return (llm, None, lambda: len([c for c in llm.calls if stage("INBOX EXTRACT")(c)]),
                lambda: len([c for c in llm.calls if stage("INBOX DIGEST")(c)]))
    sdk = FakeClaudeSDK()
    sdk.when(lambda x: "STAGE: INBOX EXTRACT" in x.prompt,
             [lambda x: call("record_followups", _extract_items(x.prompt)), say("ok")], repeat=True)
    sdk.when(lambda x: "STAGE: INBOX DIGEST" in x.prompt,
             [lambda x: call("summarize_threads", summarize_valid(x.prompt)), say("ok")], repeat=True)
    return (None, sdk, lambda: len(sdk.matching(lambda x: "STAGE: INBOX EXTRACT" in x.prompt)),
            lambda: len(sdk.matching(lambda x: "STAGE: INBOX DIGEST" in x.prompt)))


@pytest.mark.parametrize("kind", ["api", "subscription"])
def test_window_digest_of_already_scanned_mail(registry, rules, kind):
    llm, sdk, n_extract, n_digest = _backend(kind)
    clock = {"now": NOW}

    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(cc_mailbox()), llm=llm, sdk=sdk,
                                          clock=lambda: clock["now"], db=paths.local_dir() / "atlas.db")
        await scan(inbox)  # extraction creates the REQUEST_TO_ME for d1 (and the scan's own digest)
        clock["now"] = NOW + timedelta(hours=1)
        mid2 = await scan(inbox)  # nothing new: no digest
        before = (dict(inbox.state.processed), inbox.state.last_scan, n_extract(), n_digest())
        clock["now"] = NOW + timedelta(hours=2)
        mission = await inbox.start_digest(days=2)
        await asyncio.wait_for(inbox.wait(), 5)
        return store, inbox, mid2, before, mission

    store, inbox, mid2, before, mission = asyncio.run(go())
    processed, last_scan, extract_calls, digest_calls = before
    assert len(store.digests()) == 2
    assert not [d for d in store.digests() if d.mission_id == mid2]
    assert "CC digest" not in store.mission_reports_for(mid2)[0].executive_summary  # 0 new: no run, no line
    d = store.digests(limit=1)[0]
    assert d.mission_id == mission.id and mission.objective == "CC digest · last 2 days"
    assert {t.conversation_id for t in d.threads} == {"c-pol", "comité de crédito", "c9"} and d.skipped == 4
    assert d.window_end - d.window_start == timedelta(days=2)
    # only the digest step ran: no extraction, processed ids / last_scan untouched
    assert n_extract() == extract_calls and n_digest() == digest_calls + 1
    assert inbox.state.processed == processed and inbox.state.last_scan == last_scan
    # asks_me reused the scan's follow-up (same conversation): no duplicate
    asks = store.followups(kind="REQUEST_TO_ME")
    assert len(asks) == 1 and d.threads[0].followup_id == asks[0].id
    report = store.mission_reports_for(mission.id)[0]
    assert report.executive_summary.startswith(
        "CC digest · last 2 days: 9 CC candidate(s) · 4 skipped · 3 thread(s)")
    assert report.objective_status == "ACHIEVED" and store.mission(mission.id).phase == "CLOSED"
    reads = [e for e in store.evidence_for(mission.id) if e.kind == "email_read"]
    assert len(reads) == 6 and all(e.detail.startswith("from ") for e in reads)  # d11 is read, then excluded
    assert SECRET not in store.snapshot().model_dump_json()
    assert all(SECRET not in e.model_dump_json() for e in store.bus.history())
    assert SECRET.encode() not in everything_on_disk(paths.local_dir())


@pytest.mark.parametrize("kind", ["api", "subscription"])
def test_window_digest_zero_candidates_explained(registry, monkeypatch, kind):
    monkeypatch.setenv("ATLAS_MAIL_ME", ALIAS)
    llm, sdk, _n_extract, n_digest = _backend(kind)
    box = [x for x in cc_mailbox() if x[0].id in ("d5", "d6")]  # to me, and mine

    async def go():
        store, _live, inbox = make_engine(registry, FakeSource(box), llm=llm, sdk=sdk)
        mission = await inbox.start_digest(days=1)
        await asyncio.wait_for(inbox.wait(), 5)
        return store, mission.id

    store, mid = asyncio.run(go())
    assert store.digests() == [] and n_digest() == 0
    assert "CC digest · No CC emails in the last 1 day" in [e.summary for e in store.bus.history(mission_id=mid)]
    report = store.mission_reports_for(mid)[0]
    assert report.executive_summary == ("CC digest · last 1 day: 0 CC candidate(s) · 0 skipped · 0 thread(s) · "
                                        "No CC emails in the last 1 day")


def test_digest_run_shares_the_scan_lock(registry, rules):
    gate = asyncio.Event()

    class Slow(FakeSource):
        async def list_messages(self, since, limit=200):
            await gate.wait()
            return await super().list_messages(since, limit)

    async def go():
        _store, _live, inbox = make_engine(registry, Slow(cc_mailbox()), llm=digest_llm())
        await inbox.start_scan()
        with pytest.raises(ScanBusyError):
            await inbox.start_digest(days=1)
        gate.set()
        await asyncio.wait_for(inbox.wait(), 5)
        gate.clear()
        await inbox.start_digest(days=3)
        with pytest.raises(ScanBusyError):
            await inbox.start_scan()
        gate.set()
        await asyncio.wait_for(inbox.wait(), 5)

    asyncio.run(go())


def test_http_digest_run(monkeypatch, rules):
    import threading

    from atlas.main import app

    gate = threading.Event()

    class Slow(FakeSource):
        async def list_messages(self, since, limit=200):
            while not gate.is_set():
                await asyncio.sleep(0.005)
            return await super().list_messages(since, limit)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        r = client.post("/digests/run", json={"days": 2})
        assert r.status_code == 409 and "ATLAS_MS_CLIENT_ID" in r.json()["detail"]
        inbox = app.state.inbox
        inbox.source = Slow(cc_mailbox())
        inbox.clock = lambda: NOW
        assert client.post("/digests/run").status_code == 422  # no LLM backend
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        app.state.live.llm = digest_llm()
        assert client.post("/digests/run", json={"days": 15}).status_code == 422
        assert client.post("/digests/run", json={"days": 0}).status_code == 422
        r = client.post("/digests/run", json={"days": 3})
        assert r.status_code == 200 and r.json()["objective"] == "CC digest · last 3 days"
        mid = r.json()["id"]
        busy = client.post("/inbox/scan")
        assert busy.status_code == 409 and "already running" in busy.json()["detail"]
        assert client.post("/digests/run").status_code == 409
        assert client.get("/inbox/status").json()["scan_mission_id"] == mid
        gate.set()
        _poll(client, lambda st: next(m for m in st.missions if m.id == mid).phase == "CLOSED")
        digests = client.get("/digests").json()
        assert len(digests) == 1 and digests[0]["mission_id"] == mid and len(digests[0]["threads"]) == 3
        r = client.post("/digests/run")  # no body: 1 day
        assert r.status_code == 200 and r.json()["objective"] == "CC digest · last 1 day"
