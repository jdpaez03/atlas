"""Lead: the Suite client signs in through Entra (shared MSAL app/cache) when ATLAS_SUITE_SCOPE is set."""

import asyncio

import httpx
import pytest

from atlas.argos import suite_auth
from atlas.argos.checks import CheckNotConfigured
from atlas.argos.suite import SuiteClient, SuiteError

SCOPE = "api://suite-app-id/access_as_user"


class FakeMsal:
    def __init__(self, *, consented: bool, accounts=({"username": "jd@example.mx"},)):
        self.consented = consented
        self.accounts = list(accounts)
        self.asked: list = []

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        self.asked.append(tuple(scopes))
        if self.consented and scopes == [SCOPE]:
            return {"access_token": "suite-token-123"}
        return {"error": "invalid_grant", "error_description": "AADSTS65001: consent required"}

    def initiate_device_flow(self, scopes):
        assert scopes == [SCOPE]
        return {"user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900, "message": "go"}

    def acquire_token_by_device_flow(self, flow):
        self.consented = True
        return {"access_token": "t", "id_token_claims": {"preferred_username": "jd@example.mx"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ATLAS_SUITE_URL", "https://suite.example/api")
    monkeypatch.setenv("ATLAS_SUITE_SCOPE", SCOPE)
    monkeypatch.delenv("ATLAS_SUITE_TOKEN", raising=False)
    yield
    suite_auth.reset_auth(None)


def test_bearer_comes_from_entra_and_is_sent():
    suite_auth.reset_auth(suite_auth.SuiteAuth(app=FakeMsal(consented=True)))
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[{"id": 1, "titulo": "x"}])

    client = SuiteClient.from_env(transport=httpx.MockTransport(handler))
    data = asyncio.run(client.get("/l10/issues"))
    assert data == [{"id": 1, "titulo": "x"}] and seen["auth"] == "Bearer suite-token-123"


def test_no_consent_is_not_configured_with_connect_hint_then_device_flow_fixes_it():
    fake = FakeMsal(consented=False)
    suite_auth.reset_auth(suite_auth.SuiteAuth(app=fake))
    client = SuiteClient.from_env(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(CheckNotConfigured) as exc:
        asyncio.run(client.get("/l10/issues"))
    assert "Connect PAGA Suite" in exc.value.hint

    async def connect():
        auth = suite_auth.get_auth()
        flow = await auth.start()
        await auth._flow_task
        return flow, auth.poll()

    flow, status = asyncio.run(connect())
    assert flow["user_code"] == "ABCD-1234" and status["state"] == "connected"
    assert asyncio.run(client.get("/l10/issues")) == []


def test_rejected_token_explains_audience():
    suite_auth.reset_auth(suite_auth.SuiteAuth(app=FakeMsal(consented=True)))
    client = SuiteClient.from_env(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(SuiteError, match="audience"):
        asyncio.run(client.get("/l10/issues"))


def test_not_configured_without_scope_or_token(monkeypatch):
    monkeypatch.delenv("ATLAS_SUITE_SCOPE")
    with pytest.raises(CheckNotConfigured):
        SuiteClient.from_env()
    monkeypatch.setenv("ATLAS_SUITE_TOKEN", "static")  # the static-token fallback still works
    assert SuiteClient.from_env().auth is None
