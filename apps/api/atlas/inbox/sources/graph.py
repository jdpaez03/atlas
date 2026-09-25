"""GraphSource: Microsoft 365 / Outlook mail through Microsoft Graph (docs/INBOX.md §1, docs/INBOX_SETUP.md A).

Auth: MSAL public client + device code flow (delegated). The token cache lives at
`<ATLAS_LOCAL_DIR>/inbox/msal_cache.bin`, encrypted with DPAPI on Windows (msal-extensions), a plain file elsewhere.
Every Graph call gets its token through acquire_token_silent (MSAL refreshes it as needed).

Scopes: Mail.Read, plus Mail.ReadWrite for Outlook drafts when ATLAS_MS_DRAFTS=1 (the default). If the organization
won't grant Mail.ReadWrite, the source falls back to read-only (drafts are then exported as .eml by the engine).

Privacy: message bodies are returned to the caller and never logged or written to disk.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from atlas.core import paths
from atlas.core.models import EmailDraft

from .base import (
    AttachmentMeta,
    MailMessage,
    MailSourceError,
    SourceStatus,
    address_of,
    format_address,
    make_preview,
    my_addresses,
    trim_quoted,
)

log = logging.getLogger("atlas.inbox.graph")

GRAPH = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ["Mail.Read"]
DRAFT_SCOPES = ["Mail.ReadWrite"]
PAGE_SIZE = 100
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0
SELECT = (
    "id,internetMessageId,conversationId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,sentDateTime,webLink,bodyPreview,hasAttachments"
)
FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"

ADMIN_HINT = (
    "Your organization requires admin approval for this app — ask IT, or use the Power Automate folder option "
    "(docs/INBOX_SETUP.md)"
)
_ADMIN_MARKERS = ("aadsts65001", "aadsts90094", "aadsts50105", "insufficient privileges", "admin approval",
                  "need admin", "aadsts90095", "aadsts65004")  # fmt: skip
_OTHER_HINTS = [
    ("aadsts7000218", ("In the Entra app registration, open Authentication and set "
                       "'Allow public client flows' to Yes, then connect again.")),
    ("aadsts700016", ("The app (ATLAS_MS_CLIENT_ID) was not found in your organization. Check the client id and "
                      "ATLAS_MS_TENANT_ID.")),
    ("aadsts90002", ("The tenant in ATLAS_MS_TENANT_ID was not found. Use your organization's tenant id or "
                     "'organizations'.")),
    ("expired_token", "The sign-in code expired. Click Connect again and enter the new code within 15 minutes."),
    ("code_expired", "The sign-in code expired. Click Connect again and enter the new code within 15 minutes."),
    ("authorization_declined", "Sign-in was cancelled. Click Connect to try again."),
    ("access_denied", "Sign-in was cancelled or denied. Click Connect to try again."),
]  # fmt: skip


def hint_for(text: str | None) -> str | None:
    """A plain-language next step for an MSAL / Graph error text."""
    low = (text or "").lower()
    if any(m in low for m in _ADMIN_MARKERS):
        return ADMIN_HINT
    for marker, hint in _OTHER_HINTS:
        if marker in low:
            return hint
    return None


def _is_consent_error(text: str | None) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _ADMIN_MARKERS) or "consent_required" in low or "invalid_grant" in low


def _err_text(result: dict | None) -> str:
    if not result:
        return ""
    return " ".join(str(result.get(k) or "") for k in ("error", "error_description", "suberror")).strip()


# ---------------------------------------------------------------------------
# MSAL wiring
# ---------------------------------------------------------------------------


def cache_path() -> Path:
    return paths.local_dir() / "inbox" / "msal_cache.bin"


def build_token_cache(location: Path):
    """PersistedTokenCache: DPAPI-encrypted on Windows, a plain file elsewhere (logged)."""
    location.parent.mkdir(parents=True, exist_ok=True)
    try:
        from msal_extensions import FilePersistence, PersistedTokenCache
    except ImportError:  # pragma: no cover - dependency is declared
        import msal

        log.warning("msal-extensions not installed: the Microsoft token cache is kept in memory only")
        return msal.SerializableTokenCache()
    if sys.platform == "win32":
        from msal_extensions import FilePersistenceWithDataProtection

        persistence = FilePersistenceWithDataProtection(str(location))
    else:
        # No DPAPI off Windows: the cache is a plain file readable only by the ATLAS user (folder 700, file 600).
        try:
            os.chmod(location.parent, 0o700)
            if not location.exists():
                location.touch(mode=0o600)
            os.chmod(location, 0o600)
        except OSError:
            log.warning("could not restrict the permissions of %s", location)
        log.info("Microsoft token cache at %s (plain file, owner-only permissions)", location)
        persistence = FilePersistence(str(location))
    return PersistedTokenCache(persistence)


def build_msal_app(client_id: str, tenant: str):
    import msal

    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=build_token_cache(cache_path()),
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _recipients(values: list[str]) -> list[dict]:
    out = []
    for value in values:
        addr = address_of(value)
        if not addr:
            continue
        m = re.match(r"\s*\"?([^\"<]*?)\"?\s*<", value)
        name = m.group(1).strip() if m else ""
        out.append({"emailAddress": {"address": addr, **({"name": name} if name else {})}})
    return out


def _addr(obj: dict | None) -> str:
    ea = (obj or {}).get("emailAddress") or {}
    return format_address(ea.get("name"), ea.get("address"))


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _text_to_html(text: str) -> str:
    paras = html.escape(text.replace("\r\n", "\n")).split("\n")
    return "<div>" + "<br>".join(paras) + "</div>"


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


class GraphSource:
    name = "graph"

    def __init__(
        self,
        client_id: str | None = None,
        tenant: str | None = None,
        *,
        drafts: bool | None = None,
        app: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 30.0,
    ):
        self.client_id = client_id or os.getenv("ATLAS_MS_CLIENT_ID", "").strip()
        self.tenant = tenant or os.getenv("ATLAS_MS_TENANT_ID", "").strip() or "organizations"
        self.want_drafts = _env_flag("ATLAS_MS_DRAFTS", True) if drafts is None else drafts
        self._app = app
        self._transport = transport
        self._sleep = sleep
        self._timeout = httpx.Timeout(timeout, connect=10.0)
        self.read_only = not self.want_drafts  # flips to True if Mail.ReadWrite is refused
        self.read_only_reason: str | None = None
        self._flow: dict | None = None  # device-code flow in progress (MSAL's dict)
        self._flow_task: asyncio.Task | None = None
        self._flow_state = "idle"  # idle | pending | connected | failed
        self._last_error: str | None = None
        self._last_hint: str | None = None
        self._account_address: str | None = None

    # -- MSAL -------------------------------------------------------------------------------------------------

    @property
    def app(self):
        if self._app is None:
            if not self.client_id:
                raise MailSourceError(
                    "ATLAS_MS_CLIENT_ID is not set",
                    hint="Register an app in Microsoft Entra and set ATLAS_MS_CLIENT_ID (docs/INBOX_SETUP.md).",
                )
            self._app = build_msal_app(self.client_id, self.tenant)
        return self._app

    def _scopes(self) -> list[str]:
        return READ_SCOPES + ([] if self.read_only else DRAFT_SCOPES)

    def _login_scopes(self) -> list[str]:
        """Scopes asked at sign-in: mail, plus OneDrive/SharePoint read when a node has onedrive:/sharepoint: file
        roots (one sign-in serves both; silent calls then ask each for its own scopes)."""
        from atlas.live.graphfiles import FILE_SCOPES, remote_roots_configured

        return self._scopes() + (FILE_SCOPES if remote_roots_configured() else [])

    def _account(self) -> dict | None:
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        if self._account_address:
            for acc in accounts:
                if (acc.get("username") or "").lower() == self._account_address:
                    return acc
        return accounts[0]

    def _silent_sync(self) -> str | None:
        account = self._account()
        if account is None:
            return None
        self._account_address = (account.get("username") or "").lower() or None
        result = self.app.acquire_token_silent(self._scopes(), account=account)
        if result and "access_token" in result:
            self._note_granted(result)
            return result["access_token"]
        if not self.read_only:  # Mail.ReadWrite may not have been granted: try read-only
            ro = self.app.acquire_token_silent(READ_SCOPES, account=account)
            if ro and "access_token" in ro:
                self._go_read_only("Mail.ReadWrite was not granted")
                return ro["access_token"]
        if result and "error" in result:
            self._last_error = _err_text(result)
            self._last_hint = hint_for(self._last_error)
        return None

    def _note_granted(self, result: dict) -> None:
        granted = (result.get("scope") or "")
        granted = granted if isinstance(granted, str) else " ".join(granted)
        if not self.read_only and granted and "mail.readwrite" not in granted.lower():
            self._go_read_only("Mail.ReadWrite was not granted")

    def _go_read_only(self, reason: str) -> None:
        if not self.read_only:
            log.info("Graph mail source is read-only: %s", reason)
        self.read_only = True
        self.read_only_reason = reason

    async def _token(self) -> str:
        token = await asyncio.to_thread(self._silent_sync)
        if not token:
            raise MailSourceError(
                "Not connected to Microsoft 365",
                hint=self._last_hint or "Click Connect and sign in with the code shown (docs/INBOX_SETUP.md).",
            )
        return token

    # -- device code flow -------------------------------------------------------------------------------------

    async def start_device_flow(self) -> dict:
        """Start sign-in. Returns {user_code, verification_uri, expires_in, message}; completion runs in the background."""
        current = asyncio.current_task()
        if self._flow_task and not self._flow_task.done() and self._flow_task is not current:
            self._flow_task.cancel()
        flow = await asyncio.to_thread(self.app.initiate_device_flow, scopes=self._login_scopes())
        if "user_code" not in flow:
            err = _err_text(flow)
            if not self.read_only and _is_consent_error(err):
                self._go_read_only("your organization did not allow Mail.ReadWrite")
                flow = await asyncio.to_thread(self.app.initiate_device_flow, scopes=self._login_scopes())
            if "user_code" not in flow:
                err = _err_text(flow)
                self._flow_state = "failed"
                self._last_error = err
                self._last_hint = hint_for(err) or "Could not start Microsoft sign-in. Check ATLAS_MS_CLIENT_ID."
                raise MailSourceError(f"device flow failed: {err}", hint=self._last_hint)
        self._flow = flow
        self._flow_state = "pending"
        self._last_error = self._last_hint = None
        self._flow_task = asyncio.create_task(self._complete_flow(flow))
        return self._public_flow(flow)

    @staticmethod
    def _public_flow(flow: dict) -> dict:
        return {
            "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri") or "https://microsoft.com/devicelogin",
            "expires_in": int(flow.get("expires_in") or 900),
            "message": flow.get("message") or "",
        }

    async def _complete_flow(self, flow: dict) -> None:
        try:
            result = await asyncio.to_thread(self.app.acquire_token_by_device_flow, flow)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — network etc.: reported as a failed sign-in
            result = {"error": "exception", "error_description": str(exc)}
        if self._flow is not flow:
            return  # superseded by a newer flow
        if result and "access_token" in result:
            claims = result.get("id_token_claims") or {}
            self._account_address = (claims.get("preferred_username") or "").lower() or self._account_address
            self._note_granted(result)
            self._flow_state = "connected"
            self._flow = None
            return
        err = _err_text(result)
        if not self.read_only and _is_consent_error(err):
            # Mail.ReadWrite needs approval the user can't give: start over read-only with a new code.
            self._go_read_only("your organization did not allow Mail.ReadWrite (Outlook drafts); connecting read-only")
            try:
                await self.start_device_flow()
                return
            except MailSourceError:
                pass
        self._flow_state = "failed"
        self._flow = None
        self._last_error = err
        self._last_hint = hint_for(err) or "Sign-in did not complete. Click Connect to try again."
        log.warning("Microsoft sign-in failed: %s", err)

    async def poll_device_flow(self) -> dict:
        """{state: idle|pending|connected|failed, flow?, account?, error?, hint?, read_only}."""
        out: dict[str, Any] = {"state": self._flow_state, "read_only": self.read_only}
        if self._flow_state == "pending" and self._flow:
            out["flow"] = self._public_flow(self._flow)
            if self.read_only_reason:
                out["detail"] = f"Read-only: {self.read_only_reason}. Enter this new code."
        if self._flow_state == "connected":
            out["account"] = self._account_address
        if self._flow_state == "failed":
            out["error"] = self._last_error
            out["hint"] = self._last_hint
        return out

    # -- MailSource -------------------------------------------------------------------------------------------

    async def status(self) -> SourceStatus:
        if not self.client_id and self._app is None:
            return SourceStatus(
                name=self.name,
                connected=False,
                detail="ATLAS_MS_CLIENT_ID is not set.",
                hint="Register an app in Microsoft Entra and set ATLAS_MS_CLIENT_ID (docs/INBOX_SETUP.md).",
            )
        pending = self._public_flow(self._flow) if self._flow_state == "pending" and self._flow else None
        try:
            token = await asyncio.to_thread(self._silent_sync)
        except Exception as exc:  # noqa: BLE001 — status must always answer
            token = None
            self._last_error = str(exc)
        ro_note = f" Read-only ({self.read_only_reason}); drafts are exported as .eml." if self.read_only_reason else ""
        if token:
            return SourceStatus(
                name=self.name,
                connected=True,
                account=self._account_address,
                detail=("Connected to Microsoft 365." + ro_note).strip(),
                can_draft=not self.read_only,
            )
        if pending:
            return SourceStatus(
                name=self.name,
                connected=False,
                detail=(f"Waiting for sign-in: enter code {pending['user_code']} at "
                        f"{pending['verification_uri']}." + ro_note),
                pending=pending,
            )  # fmt: skip
        return SourceStatus(
            name=self.name,
            connected=False,
            detail=("Not connected." + (f" Last error: {self._last_error}" if self._last_error else "")).strip(),
            hint=self._last_hint or "Click Connect and sign in with your work account.",
        )

    async def list_messages(self, since: datetime, limit: int = 200) -> list[MailMessage]:
        since_utc = (since if since.tzinfo else since.replace(tzinfo=UTC)).astimezone(UTC)
        stamp = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        me = my_addresses()
        found: dict[str, MailMessage] = {}
        async with self._client() as client:
            for folder in ("inbox", "sentitems"):
                url: str | None = f"{GRAPH}/me/mailFolders/{folder}/messages"
                params: dict | None = {
                    "$filter": f"receivedDateTime ge {stamp}",
                    "$select": SELECT,
                    "$orderby": "receivedDateTime desc",
                    "$top": str(min(PAGE_SIZE, max(1, limit))),
                }
                taken = 0
                while url and taken < limit:
                    data = (await self._request(client, "GET", url, params=params)).json()
                    params = None  # nextLink carries the query
                    for item in data.get("value", []):
                        msg = self._to_message(item, sent=folder == "sentitems", me=me)
                        found.setdefault(msg.id, msg)
                        taken += 1
                    url = data.get("@odata.nextLink")
        return sorted(found.values(), key=lambda m: m.received_at, reverse=True)[:limit]

    def _to_message(self, item: dict, *, sent: bool, me: set[str]) -> MailMessage:
        sender = _addr(item.get("from"))
        mine = me | ({self._account_address} if self._account_address else set())
        when = item.get("sentDateTime") if sent else item.get("receivedDateTime")
        return MailMessage(
            id=item["id"],
            internet_message_id=item.get("internetMessageId"),
            conversation_id=item.get("conversationId"),
            subject=(item.get("subject") or "").strip() or "(no subject)",
            sender=sender,
            to=[_addr(r) for r in item.get("toRecipients") or []],
            cc=[_addr(r) for r in item.get("ccRecipients") or []],
            received_at=_parse_dt(when or item.get("receivedDateTime")),
            is_from_me=sent or address_of(sender) in mine,
            web_link=item.get("webLink"),
            preview=make_preview(item.get("bodyPreview") or ""),
            has_attachments=bool(item.get("hasAttachments")),
        )

    async def get_body(self, message_id: str) -> str:
        async with self._client() as client:
            resp = await self._request(
                client,
                "GET",
                f"{GRAPH}/me/messages/{message_id}",
                params={"$select": "body,uniqueBody"},
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
        data = resp.json()
        unique = ((data.get("uniqueBody") or {}).get("content") or "").strip()
        text = unique or (data.get("body") or {}).get("content") or ""
        return trim_quoted(text)

    async def list_attachments(self, message_id: str) -> list[AttachmentMeta]:
        """File attachments only: inline images, attached Outlook items and reference (cloud) attachments are
        skipped."""
        out: list[AttachmentMeta] = []
        async with self._client() as client:
            url: str | None = f"{GRAPH}/me/messages/{message_id}/attachments"
            params: dict | None = {"$select": "id,name,size,contentType,isInline"}
            while url:
                data = (await self._request(client, "GET", url, params=params)).json()
                params = None
                for item in data.get("value", []):
                    kind = item.get("@odata.type") or FILE_ATTACHMENT  # $select may drop it on some tenants
                    if kind != FILE_ATTACHMENT or item.get("isInline") or not item.get("id"):
                        continue
                    out.append(AttachmentMeta(
                        id=str(item["id"]), name=(item.get("name") or "").strip() or "attachment",
                        size=int(item.get("size") or 0), content_type=item.get("contentType") or "",
                    ))
                url = data.get("@odata.nextLink")
        return out

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        async with self._client() as client:
            resp = await self._request(client, "GET", f"{GRAPH}/me/messages/{message_id}/attachments/"
                                       f"{attachment_id}/$value")  # fmt: skip
        return resp.content

    async def create_outlook_draft(self, draft: EmailDraft) -> str | None:
        if self.read_only:
            return None
        try:
            async with self._client() as client:
                return await self._create_draft(client, draft)
        except MailSourceError as exc:
            if "403" in str(exc) or "accessdenied" in str(exc).lower():
                self._go_read_only("Outlook refused to save drafts (Mail.ReadWrite not granted)")
                return None
            raise

    async def _create_draft(self, client: httpx.AsyncClient, draft: EmailDraft) -> str | None:
        patch: dict[str, Any] = {"subject": draft.subject}
        if draft.to:
            patch["toRecipients"] = _recipients(draft.to)
        if draft.cc:
            patch["ccRecipients"] = _recipients(draft.cc)
        if draft.in_reply_to:
            try:
                reply = (
                    await self._request(client, "POST", f"{GRAPH}/me/messages/{draft.in_reply_to}/createReply",
                                        json={})
                ).json()  # fmt: skip
            except MailSourceError as exc:
                if "404" not in str(exc):
                    raise
                reply = None  # the source message is gone: save a new draft instead
            if reply and reply.get("id"):
                original = ((reply.get("body") or {}).get("content") or "") if isinstance(reply.get("body"), dict) else ""
                ours = _text_to_html(draft.body)
                if original and re.search(r"<body[^>]*>", original, re.IGNORECASE):
                    content = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + ours + "<br>", original, count=1,
                                     flags=re.IGNORECASE)  # fmt: skip
                else:
                    content = ours + ("<br>" + original if original else "")
                patch["body"] = {"contentType": "HTML", "content": content}
                updated = (await self._request(client, "PATCH", f"{GRAPH}/me/messages/{reply['id']}",
                                               json=patch)).json()  # fmt: skip
                return updated.get("webLink") or reply.get("webLink")
        patch["body"] = {"contentType": "Text", "content": draft.body}
        created = (await self._request(client, "POST", f"{GRAPH}/me/messages", json=patch)).json()
        return created.get("webLink")

    # -- HTTP -------------------------------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", None) or {})
        attempt = 0
        while True:
            headers["Authorization"] = f"Bearer {await self._token()}"
            try:
                resp = await client.request(method, url, headers=headers, **kw)
            except httpx.TimeoutException as exc:
                if attempt >= MAX_RETRIES:
                    raise MailSourceError(f"Microsoft Graph timed out: {exc}", hint="Check the network and retry.")
                attempt += 1
                await self._sleep(min(2.0**attempt, MAX_RETRY_WAIT))
                continue
            except httpx.HTTPError as exc:
                raise MailSourceError(f"Microsoft Graph unreachable: {exc}", hint="Check the network and retry.")
            if resp.status_code in (429, 503, 504) and attempt < MAX_RETRIES:
                attempt += 1
                await self._sleep(self._retry_after(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise self._error(resp)
            return resp

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        raw = resp.headers.get("Retry-After")
        try:
            wait = float(raw) if raw is not None else 2.0**attempt
        except ValueError:
            wait = 2.0**attempt
        return max(0.0, min(wait, MAX_RETRY_WAIT))

    def _error(self, resp: httpx.Response) -> MailSourceError:
        try:
            err = resp.json().get("error") or {}
            text = f"{err.get('code', '')}: {err.get('message', '')}"
        except Exception:  # noqa: BLE001 — not JSON
            text = resp.text[:300]
        hint = hint_for(text)
        if resp.status_code == 401:
            hint = hint or "Your Microsoft sign-in expired. Click Connect to sign in again."
        elif resp.status_code == 403:
            hint = hint or "Access denied by Microsoft 365. " + ADMIN_HINT
        elif resp.status_code == 429:
            hint = "Microsoft Graph is throttling requests; ATLAS will retry on the next scan."
        self._last_error, self._last_hint = text, hint
        return MailSourceError(f"Graph {resp.status_code} {text}", hint=hint)
