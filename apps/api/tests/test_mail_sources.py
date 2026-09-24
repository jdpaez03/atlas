"""Mail sources (docs/INBOX.md §1): FolderSource on fixture files, GraphSource on a fake transport + fake MSAL.

No network: Graph goes through httpx.MockTransport, MSAL is a fake app object.
.msg: extract-msg can only read Outlook .msg files (it cannot write one), so the .msg test injects a fake parsed
message through `folder._open_msg`; the file on disk is a placeholder.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from atlas.core.models import EmailDraft
from atlas.inbox import sources
from atlas.inbox.sources import folder as folder_mod
from atlas.inbox.sources.base import MailSource, MailSourceError, trim_quoted
from atlas.inbox.sources.folder import FolderSource, html_to_text
from atlas.inbox.sources.graph import ADMIN_HINT, GraphSource

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mailbox(tmp_path: Path) -> Path:
    dest = tmp_path / "ATLAS-inbox"
    shutil.copytree(FIXTURES, dest)
    return dest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_trim_quoted_variants():
    assert trim_quoted("Hola\n\nEl lun, 21 sep 2026 a las 10:00, Pedro escribió:\n> viejo") == "Hola"
    assert trim_quoted("Ok\n-----Original Message-----\nFrom: x") == "Ok"
    assert trim_quoted("Sure\nOn Mon, Sep 21, 2026 at 9:00 AM Ana <a@b.c> wrote:\nold") == "Sure"
    assert trim_quoted("Listo\n\nDe: Ana\nEnviado: lunes") == "Listo"
    assert trim_quoted("x" * 50, max_chars=10).startswith("x" * 10)
    assert trim_quoted("x" * 50, max_chars=10).endswith("[…truncated]")


def test_body_cap_from_env(monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_BODY_MAX_CHARS", "300")
    assert len(trim_quoted("y" * 1000)) < 320


def test_html_to_text_keeps_breaks():
    text = html_to_text("<style>x{}</style><p>Uno</p><p>Dos<br>Tres &amp; cuatro</p><ul><li>a</li></ul>")
    assert text.splitlines() == ["Uno", "", "Dos", "Tres & cuatro", "", "- a"]


# ---------------------------------------------------------------------------
# FolderSource
# ---------------------------------------------------------------------------


def test_folder_parses_eml_and_json(mailbox, monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_ME", "jd@empresa.com; juan@personal.com")
    src = FolderSource(mailbox)
    assert isinstance(src, MailSource)
    msgs = run(src.list_messages(SINCE))
    by_subject = {m.subject: m for m in msgs}
    assert set(by_subject) == {"RE: Cotización de materiales", "Contrato firmado", "Minuta semanal"}
    assert [m.received_at for m in msgs] == sorted((m.received_at for m in msgs), reverse=True)

    eml = by_subject["RE: Cotización de materiales"]
    assert eml.sender == "Laura Gómez <laura.gomez@proveedor.com>"
    assert eml.to == ["Juan Paez <jd@empresa.com>"] and eml.cc == ["compras@empresa.com"]
    assert eml.internet_message_id == "<abc123@proveedor.com>"
    assert eml.id.startswith("file-") and not eml.is_from_me
    assert eml.received_at.tzinfo is not None
    assert "cotización actualizada" in eml.preview and "puedes mandarme" not in eml.preview

    pa = by_subject["Contrato firmado"]
    assert pa.id == "AAMkAGpa002=" and pa.conversation_id == "AAQkConv888"
    assert pa.cc == ["legal@cliente.com", "Ana <ana@cliente.com>"]
    assert pa.web_link and pa.web_link.startswith("https://outlook.office365.com/")
    assert not pa.is_from_me

    sent = by_subject["Minuta semanal"]
    assert sent.is_from_me and sent.to == ["equipo@empresa.com", "Pedro Ruiz <pedro@empresa.com>"]

    body = run(src.get_body(eml.id))
    assert "Te envío la cotización actualizada el viernes 25.\nSaludos," in body
    assert "puedes mandarme" not in body and "From:" not in body
    assert run(src.get_body("AAMkAGpa002=")) == "Juan, adjunto el contrato.\nQuedo atento a tus comentarios antes del 30."
    assert "old stuff" not in run(src.get_body("AAMkAGsent001="))


def test_folder_is_from_me_by_address(mailbox, monkeypatch):
    monkeypatch.setenv("ATLAS_MAIL_ME", "JD@empresa.com")
    shutil.move(mailbox / "sent" / "weekly.json", mailbox / "weekly.json")
    msgs = run(FolderSource(mailbox).list_messages(SINCE))
    assert {m.subject: m.is_from_me for m in msgs}["Minuta semanal"] is True


def test_folder_since_filter_and_readonly(mailbox):
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in mailbox.rglob("*") if p.is_file()}
    src = FolderSource(mailbox, me=set())
    msgs = run(src.list_messages(datetime(2026, 9, 23, 12, 0, tzinfo=UTC)))
    assert {m.subject for m in msgs} == {"Contrato firmado", "Minuta semanal"}
    assert run(src.list_messages(datetime(2999, 1, 1, tzinfo=UTC))) == []  # newer than every file mtime
    assert len(run(src.list_messages(SINCE, limit=1))) == 1
    for m in run(src.list_messages(SINCE)):
        run(src.get_body(m.id))
    assert run(src.create_outlook_draft(EmailDraft(subject="x", body="y"))) is None
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in mailbox.rglob("*") if p.is_file()}
    assert after == before  # nothing modified, added or deleted


def test_folder_ids_stable_and_bad_files_skipped(mailbox):
    (mailbox / "broken.json").write_text("{not json", encoding="utf-8")
    (mailbox / "notes.txt").write_text("ignored", encoding="utf-8")
    ids1 = {m.id for m in run(FolderSource(mailbox, me=set()).list_messages(SINCE))}
    ids2 = {m.id for m in run(FolderSource(mailbox, me=set()).list_messages(SINCE))}
    assert ids1 == ids2 and len(ids1) == 3
    fresh = FolderSource(mailbox, me=set())  # get_body without listing first still finds the file
    eml_id = next(i for i in ids1 if i.startswith("file-"))
    assert "Hola Juan" in run(fresh.get_body(eml_id))


def test_folder_msg_via_fake_extract_msg(mailbox, monkeypatch):
    class FakeMsg:
        subject = "Pendiente: reporte Q3"
        sender = "Marta Ruiz <marta@empresa.com>"
        to = "Juan Paez <jd@empresa.com>"
        cc = None
        date = datetime(2026, 9, 22, 10, 0)  # noqa: DTZ001 — naive → UTC
        body = ""
        htmlBody = b"<p>Me comprometo a mandar el reporte.</p><p>-----Original Message-----</p><p>viejo</p>"
        messageId = "<msg1@empresa.com>"
        closed = False

        def close(self):
            FakeMsg.closed = True

    (mailbox / "q3.msg").write_bytes(b"placeholder: extract-msg cannot write .msg files")
    monkeypatch.setattr(folder_mod, "_open_msg", lambda path: FakeMsg())
    src = FolderSource(mailbox, me={"jd@empresa.com"})
    msg = next(m for m in run(src.list_messages(SINCE)) if m.subject == "Pendiente: reporte Q3")
    assert msg.sender == "Marta Ruiz <marta@empresa.com>" and msg.cc == []
    assert msg.received_at == datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    assert run(src.get_body(msg.id)) == "Me comprometo a mandar el reporte."
    assert FakeMsg.closed


def test_folder_status(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_MAIL_FOLDER", raising=False)
    st = run(FolderSource().status())
    assert not st.connected and "ATLAS_MAIL_FOLDER" in st.hint
    st = run(FolderSource(tmp_path / "missing").status())
    assert not st.connected and "not found" in st.detail
    shutil.copytree(FIXTURES, tmp_path / "box")
    st = run(FolderSource(tmp_path / "box", me=set()).status())
    assert st.connected and not st.can_draft and "3 mail files" in st.detail and "ATLAS_MAIL_ME" in st.hint


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_make_source(monkeypatch, tmp_path):
    for k in ("ATLAS_MAIL_SOURCE", "ATLAS_MS_CLIENT_ID", "ATLAS_MAIL_FOLDER"):
        monkeypatch.delenv(k, raising=False)
    sources.reset_source()
    assert sources.make_source() is None
    monkeypatch.setenv("ATLAS_MAIL_FOLDER", str(tmp_path))
    src = sources.make_source()
    assert isinstance(src, FolderSource) and sources.make_source() is src
    monkeypatch.setenv("ATLAS_MS_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    assert isinstance(sources.make_source(), GraphSource)
    monkeypatch.setenv("ATLAS_MAIL_SOURCE", "folder")
    assert isinstance(sources.make_source(), FolderSource)
    monkeypatch.setenv("ATLAS_MAIL_SOURCE", "imap")
    with pytest.raises(ValueError):
        sources.make_source()
    sources.reset_source()


# ---------------------------------------------------------------------------
# GraphSource
# ---------------------------------------------------------------------------


class FakeMsal:
    """Stands in for msal.PublicClientApplication."""

    def __init__(self, *, granted=("Mail.Read", "Mail.ReadWrite"), flow_result=None, init_error=None):
        self.granted = set(granted)
        self.accounts: list[dict] = []
        self.flow_result = flow_result
        self.init_error = init_error
        self.flows: list[list[str]] = []
        self.silent_calls = 0

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls += 1
        if not self.accounts:
            return None
        if not set(scopes) <= self.granted:
            return {"error": "invalid_grant", "error_description": "AADSTS65001: consent required"}
        return {"access_token": "tok", "scope": " ".join(sorted(self.granted))}

    def initiate_device_flow(self, scopes):
        self.flows.append(list(scopes))
        if self.init_error:
            return {"error": "invalid_client", "error_description": self.init_error}
        return {
            "user_code": f"CODE{len(self.flows)}",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "message": "To sign in, use a web browser...",
            "device_code": "dev",
            "_scopes": list(scopes),
        }

    def acquire_token_by_device_flow(self, flow):
        result = self.flow_result(flow) if callable(self.flow_result) else self.flow_result
        if result and "access_token" in result:
            self.accounts = [{"username": "JD@empresa.com"}]
        return result


def connected_app(**kw) -> FakeMsal:
    app = FakeMsal(**kw)
    app.accounts = [{"username": "jd@empresa.com"}]
    return app


def graph_msg(i: int, *, sender="ana@cliente.com", when="2026-09-23T10:00:00Z") -> dict:
    return {
        "id": f"id{i}",
        "internetMessageId": f"<m{i}@x>",
        "conversationId": "conv",
        "subject": f"Asunto {i}",
        "from": {"emailAddress": {"name": "Ana", "address": sender}},
        "toRecipients": [{"emailAddress": {"name": "Juan", "address": "jd@empresa.com"}}],
        "ccRecipients": [],
        "receivedDateTime": when,
        "sentDateTime": when,
        "webLink": f"https://outlook.office365.com/owa/?ItemID=id{i}",
        "bodyPreview": "hola " * 100,
    }


def make_graph(handler, app=None, sleeps=None, **kw) -> GraphSource:
    async def fake_sleep(s):
        (sleeps if sleeps is not None else []).append(s)

    return GraphSource(
        "client", "organizations", app=app or connected_app(), transport=httpx.MockTransport(handler),
        sleep=fake_sleep, **kw,
    )  # fmt: skip


def test_graph_list_messages_paging_and_sent():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        path = req.url.path
        if path.endswith("/inbox/messages") and "skip" not in str(req.url):
            assert "receivedDateTime ge 2026-09-01T00:00:00Z" in req.url.params["$filter"]
            assert "bodyPreview" in req.url.params["$select"]
            return httpx.Response(200, json={
                "value": [graph_msg(1), graph_msg(2, when="2026-09-22T10:00:00Z")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=2",
            })  # fmt: skip
        if path.endswith("/inbox/messages"):
            return httpx.Response(200, json={"value": [graph_msg(3, sender="JD@empresa.com")]})
        if path.endswith("/sentitems/messages"):
            return httpx.Response(200, json={"value": [graph_msg(4, sender="jd@empresa.com",
                                                                 when="2026-09-24T09:00:00Z")]})  # fmt: skip
        return httpx.Response(404)

    src = make_graph(handler)
    msgs = run(src.list_messages(SINCE))
    assert msgs[0].id == "id4" and {m.id for m in msgs} == {"id1", "id2", "id3", "id4"}
    by = {m.id: m for m in msgs}
    assert by["id4"].is_from_me and by["id3"].is_from_me and not by["id1"].is_from_me
    assert by["id1"].sender == "Ana <ana@cliente.com>" and by["id1"].to == ["Juan <jd@empresa.com>"]
    assert len(by["id1"].preview) <= 255 and by["id1"].web_link
    assert len(seen) == 3
    assert len(run(src.list_messages(SINCE, limit=2))) == 2


def test_graph_retries_429_with_retry_after():
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"body": {"content": "Hola\nOn Mon, X wrote:\nold"}, "uniqueBody": {}})

    src = make_graph(handler, sleeps=sleeps)
    assert run(src.get_body("id1")) == "Hola"
    assert sleeps == [7.0, 7.0]


def test_graph_get_body_prefers_text_and_unique_body():
    def handler(req):
        assert req.headers["Prefer"] == 'outlook.body-content-type="text"'
        assert req.url.params["$select"] == "body,uniqueBody"
        return httpx.Response(200, json={"body": {"content": "full\nFrom: x"}, "uniqueBody": {"content": "solo nuevo"}})

    assert run(make_graph(handler).get_body("id1")) == "solo nuevo"


def test_graph_gives_up_after_retries_and_maps_errors():
    src = make_graph(lambda req: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(MailSourceError) as exc:
        run(src.get_body("x"))
    assert "429" in str(exc.value) and exc.value.hint

    forbidden = {"error": {"code": "ErrorAccessDenied", "message": "Access is denied. Check credentials"}}
    src = make_graph(lambda req: httpx.Response(403, json=forbidden))
    with pytest.raises(MailSourceError) as exc:
        run(src.get_body("x"))
    assert ADMIN_HINT in exc.value.hint


def test_graph_not_connected_raises_with_hint():
    src = make_graph(lambda req: httpx.Response(200, json={}), app=FakeMsal())
    with pytest.raises(MailSourceError) as exc:
        run(src.list_messages(SINCE))
    assert "Connect" in exc.value.hint
    st = run(src.status())
    assert not st.connected and st.hint


def test_graph_device_flow_success():
    app = FakeMsal(flow_result={"access_token": "tok", "scope": "Mail.Read Mail.ReadWrite",
                                "id_token_claims": {"preferred_username": "JD@empresa.com"}})  # fmt: skip
    src = make_graph(lambda req: httpx.Response(200, json={}), app=app)

    async def go():
        flow = await src.start_device_flow()
        assert flow == {"user_code": "CODE1", "verification_uri": "https://microsoft.com/devicelogin",
                        "expires_in": 900, "message": "To sign in, use a web browser..."}  # fmt: skip
        for _ in range(50):
            state = await src.poll_device_flow()
            if state["state"] != "pending":
                break
            await asyncio.sleep(0.01)
        return state, await src.status()

    state, st = run(go())
    assert app.flows == [["Mail.Read", "Mail.ReadWrite"]]
    assert state["state"] == "connected" and state["account"] == "jd@empresa.com"
    assert st.connected and st.can_draft and st.account == "jd@empresa.com"


def test_graph_device_flow_pending_status():
    import threading

    gate = threading.Event()

    def slow(flow):
        gate.wait(5)
        return {"error": "expired_token", "error_description": "AADSTS70020: expired"}

    src = make_graph(lambda req: httpx.Response(200, json={}), app=FakeMsal(flow_result=slow))

    async def go():
        await src.start_device_flow()
        st = await src.status()
        pending = await src.poll_device_flow()
        gate.set()
        for _ in range(100):
            final = await src.poll_device_flow()
            if final["state"] != "pending":
                break
            await asyncio.sleep(0.01)
        return st, pending, final

    st, pending, final = run(go())
    assert not st.connected and st.pending["user_code"] == "CODE1" and "CODE1" in st.detail
    assert pending["state"] == "pending" and pending["flow"]["user_code"] == "CODE1"
    assert final["state"] == "failed" and "expired" in final["hint"]


def test_graph_consent_error_falls_back_to_read_only():
    def result(flow):
        if "Mail.ReadWrite" in flow["_scopes"]:
            return {"error": "invalid_grant",
                    "error_description": "AADSTS65001: The user or administrator has not consented"}  # fmt: skip
        return {"access_token": "tok", "scope": "Mail.Read", "id_token_claims": {"preferred_username": "jd@empresa.com"}}

    app = FakeMsal(granted=("Mail.Read",), flow_result=result)
    src = make_graph(lambda req: httpx.Response(200, json={}), app=app)

    async def go():
        await src.start_device_flow()
        for _ in range(100):
            state = await src.poll_device_flow()
            if state["state"] == "connected":
                break
            await asyncio.sleep(0.01)
        return state, await src.status()

    state, st = run(go())
    assert app.flows == [["Mail.Read", "Mail.ReadWrite"], ["Mail.Read"]]
    assert state["state"] == "connected" and state["read_only"]
    assert st.connected and not st.can_draft and "Read-only" in st.detail
    assert run(src.create_outlook_draft(EmailDraft(subject="s", body="b"))) is None


def test_graph_admin_approval_hint():
    app = FakeMsal(flow_result={"error": "access_denied",
                                "error_description": "AADSTS90094: The grant requires admin permission."})  # fmt: skip
    src = make_graph(lambda req: httpx.Response(200, json={}), app=app, drafts=False)

    async def go():
        await src.start_device_flow()
        for _ in range(100):
            state = await src.poll_device_flow()
            if state["state"] != "pending":
                break
            await asyncio.sleep(0.01)
        return state, await src.status()

    state, st = run(go())
    assert app.flows == [["Mail.Read"]]
    assert state["state"] == "failed" and state["hint"] == ADMIN_HINT
    assert st.hint == ADMIN_HINT and not st.connected

    bad = make_graph(lambda req: httpx.Response(200), app=FakeMsal(init_error="AADSTS50105: user not assigned"),
                     drafts=False)  # fmt: skip
    with pytest.raises(MailSourceError) as exc:
        run(bad.start_device_flow())
    assert exc.value.hint == ADMIN_HINT


def test_graph_silent_read_only_after_restart():
    src = make_graph(lambda req: httpx.Response(200, json={}), app=connected_app(granted=("Mail.Read",)))
    st = run(src.status())
    assert st.connected and not st.can_draft and "Read-only" in st.detail


def test_graph_create_reply_draft():
    calls: list[tuple[str, str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        calls.append((req.method, req.url.path, body))
        if req.url.path.endswith("/messages/src1/createReply"):
            return httpx.Response(201, json={
                "id": "draft1", "webLink": "https://outlook/draft1-old",
                "body": {"contentType": "html", "content": "<html><body><div>quoted thread</div></body></html>"},
            })  # fmt: skip
        if req.method == "PATCH" and req.url.path.endswith("/messages/draft1"):
            return httpx.Response(200, json={"id": "draft1", "webLink": "https://outlook/draft1"})
        return httpx.Response(404)

    draft = EmailDraft(subject="RE: Contrato", body="Hola Carlos,\n¿Alguna novedad?", in_reply_to="src1",
                       to=["Carlos Díaz <carlos@cliente.com>"], cc=["legal@cliente.com"])  # fmt: skip
    link = run(make_graph(handler).create_outlook_draft(draft))
    assert link == "https://outlook/draft1"
    method, _, patch = calls[1]
    assert method == "PATCH" and patch["subject"] == "RE: Contrato"
    assert patch["toRecipients"] == [{"emailAddress": {"address": "carlos@cliente.com", "name": "Carlos Díaz"}}]
    assert patch["ccRecipients"] == [{"emailAddress": {"address": "legal@cliente.com"}}]
    content = patch["body"]["content"]
    assert content.index("Hola Carlos,<br>¿Alguna novedad?") < content.index("quoted thread")
    assert not any("send" in path.lower() for _, path, _ in calls)  # never sends


def test_graph_new_draft_and_403_degrades():
    def handler(req):
        assert req.method == "POST" and req.url.path == "/v1.0/me/messages"
        body = json.loads(req.content)
        assert body["body"] == {"contentType": "Text", "content": "Texto"}
        return httpx.Response(201, json={"id": "d2", "webLink": "https://outlook/d2"})

    assert run(make_graph(handler).create_outlook_draft(EmailDraft(subject="Hola", body="Texto",
                                                                   to=["a@b.com"]))) == "https://outlook/d2"  # fmt: skip

    denied = {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}
    src = make_graph(lambda req: httpx.Response(403, json=denied))
    assert run(src.create_outlook_draft(EmailDraft(subject="x", body="y"))) is None
    assert src.read_only and not run(src.status()).can_draft
