"""PAGA Suite sign-in through Microsoft Entra (the Suite's login is Entra-based).

ATLAS reuses the Microsoft sign-in it already has for Outlook: the same app registration
(`ATLAS_MS_CLIENT_ID` / `ATLAS_MS_TENANT_ID`) and the same encrypted token cache. It asks for a token whose
audience is the Suite's API (`ATLAS_SUITE_SCOPE`, e.g. `api://<suite-app-id>/access_as_user`), so the Suite sees
the user, with the user's own permissions. Read-only use only.

If the scope hasn't been consented yet, `acquire_token_silent` fails; a separate device-code sign-in for the
Suite scope (`start()` / `poll()`) records the consent in the same cache. Microsoft issues one token per
resource, so Outlook and the Suite are two consents under one account.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger("atlas.argos.suite_auth")

CONNECT_HINT = "Connect PAGA Suite in Monitor (sign in once with the code shown)"


class SuiteAuthError(RuntimeError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def configured_scope() -> str | None:
    scope = os.getenv("ATLAS_SUITE_SCOPE", "").strip()
    return scope or None


def _scopes() -> list[str]:
    scope = configured_scope()
    if not scope:
        raise SuiteAuthError("ATLAS_SUITE_SCOPE is not set",
                             "Set ATLAS_SUITE_SCOPE (the Suite API's scope, e.g. api://<id>/access_as_user)")
    return [scope]


class SuiteAuth:
    """Token provider for the Suite API, sharing the Outlook MSAL app and cache."""

    def __init__(self, app: Any | None = None):
        self._app = app
        self._flow_task: asyncio.Task | None = None
        self._flow: dict | None = None
        self._state: str = "idle"
        self._error: str | None = None
        self._account: str | None = None

    @property
    def app(self) -> Any:
        if self._app is None:
            from ..inbox.sources.graph import build_msal_app

            client_id = os.getenv("ATLAS_MS_CLIENT_ID", "").strip()
            if not client_id:
                raise SuiteAuthError("ATLAS_MS_CLIENT_ID is not set",
                                     "Set up the Microsoft sign-in first (docs/INBOX_SETUP.md)")
            self._app = build_msal_app(client_id, os.getenv("ATLAS_MS_TENANT_ID", "").strip() or "organizations")
        return self._app

    def _pick_account(self) -> dict | None:
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        me = [a.strip().lower() for a in os.getenv("ATLAS_MAIL_ME", "").split(";") if a.strip()]
        for acc in accounts:
            if (acc.get("username") or "").lower() in me:
                return acc
        return accounts[0]

    def _silent(self) -> str:
        account = self._pick_account()
        if account is None:
            raise SuiteAuthError("Not signed in to Microsoft", CONNECT_HINT)
        result = self.app.acquire_token_silent(_scopes(), account=account)
        if result and "access_token" in result:
            self._account = account.get("username")
            return result["access_token"]
        err = (result or {}).get("error_description") or (result or {}).get("error") or "no cached consent"
        raise SuiteAuthError(f"No PAGA Suite token ({str(err).splitlines()[0][:160]})", CONNECT_HINT)

    async def token(self) -> str:
        return await asyncio.to_thread(self._silent)

    # -- one-time consent for the Suite scope (device code) -----------------------------------------------

    async def start(self) -> dict:
        flow = await asyncio.to_thread(self.app.initiate_device_flow, scopes=_scopes())
        if "user_code" not in flow:
            raise SuiteAuthError(f"Could not start sign-in: {flow.get('error_description') or flow}",
                                 "Check that ATLAS's app registration has the Suite API permission")
        self._flow, self._state, self._error = flow, "pending", None
        if self._flow_task and not self._flow_task.done():
            self._flow_task.cancel()
        self._flow_task = asyncio.create_task(self._complete(flow))
        return {k: flow.get(k) for k in ("user_code", "verification_uri", "expires_in", "message")}

    async def _complete(self, flow: dict) -> None:
        try:
            result = await asyncio.to_thread(self.app.acquire_token_by_device_flow, flow)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced through poll()
            self._state, self._error = "failed", str(exc)
            return
        if "access_token" in result:
            claims = result.get("id_token_claims") or {}
            self._account = claims.get("preferred_username") or self._account
            self._state, self._error = "connected", None
        else:
            self._state = "failed"
            self._error = result.get("error_description") or result.get("error") or "sign-in failed"

    def poll(self) -> dict:
        out: dict[str, Any] = {"state": self._state, "account": self._account}
        if self._state == "pending" and self._flow:
            out["flow"] = {k: self._flow.get(k) for k in ("user_code", "verification_uri", "expires_in", "message")}
        if self._error:
            out["error"] = self._error.splitlines()[0][:300]
            if "AADSTS65001" in self._error or "consent" in self._error.lower():
                out["hint"] = ("Your organization requires admin consent for the Suite API permission — ask IT to "
                               "click 'Grant admin consent' on ATLAS's app registration")
        return out


_AUTH: SuiteAuth | None = None


def get_auth() -> SuiteAuth:
    global _AUTH
    if _AUTH is None:
        _AUTH = SuiteAuth()
    return _AUTH


def reset_auth(auth: SuiteAuth | None = None) -> None:
    global _AUTH
    _AUTH = auth
