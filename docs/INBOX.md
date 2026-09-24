# Inbox: follow-ups from work email

The goal: ATLAS reads the user's **work Outlook / Microsoft 365** mailbox. It turns commitments and pending replies into
**follow-ups on the ATLAS board**, and ALFRED **drafts** follow-up emails. ATLAS **never sends email**: the user
approves a draft, and ATLAS saves it into Outlook's Drafts folder (Graph) or exports it as an `.eml` that opens in Outlook as an
editable, unsent message.

Contracts (`core/models.py`, already in place): `EmailRef`, `FollowUp`, `EmailDraft`; events `followup.upserted`
(`{followup}`) and `draft.upserted` (`{draft}`); `WorldState.followups/drafts`; evidence kinds `email_read`,
`draft_created`. The reducers are generic (upsert by key), so no reducer changes are needed beyond registering the keys.

## Privacy rule (from the user's own dossier §14)

The agent reads a message, extracts the defined fields, and **does not keep the original**. Only an `EmailRef` is stored
(message id, subject, sender, received date, an excerpt of ≤ 400 chars, and the web link). Bodies are fetched on demand
for extraction or drafting and never written to disk or the event log. Evidence records an `email_read` item with the subject
and sender only.

## 1. Mail sources: `atlas/inbox/sources/` (builder M1)

```python
@dataclass
class MailMessage:
    id: str                 # stable id (Graph id, or file hash for folder source)
    internet_message_id: str | None
    conversation_id: str | None
    subject: str
    sender: str             # "Name <email>"
    to: list[str]; cc: list[str]
    received_at: datetime
    is_from_me: bool        # the user sent it (for AWAITING_REPLY / MY_COMMITMENT detection)
    web_link: str | None
    preview: str            # ≤ 255 chars

class MailSource(Protocol):
    name: str               # "graph" | "folder"
    async def status(self) -> SourceStatus            # connected?, account, detail/hint
    async def list_messages(self, since: datetime, limit: int = 200) -> list[MailMessage]  # inbox + sent items
    async def get_body(self, message_id: str) -> str  # plain text (HTML → text), quoted history trimmed
    async def create_outlook_draft(self, draft: EmailDraft) -> str | None   # returns web link; None if unsupported
```

- **GraphSource**: MSAL public-client **device code flow** (`POST /inbox/connect` returns `{user_code, verification_uri, expires_in}`; `GET /inbox/connect/status` polls). Delegated scopes: `Mail.Read` (required) and `Mail.ReadWrite` (optional, only for Outlook drafts; degrades to `.eml` if not granted). Settings: `ATLAS_MS_CLIENT_ID` and `ATLAS_MS_TENANT_ID` (default `organizations`). The token cache is `atlas-local/inbox/msal_cache.bin`, and it uses Windows DPAPI via `msal-extensions` when available. Graph endpoints: `/me/mailFolders/inbox/messages`, `/me/mailFolders/sentitems/messages` with `$filter=receivedDateTime ge …`, `$select`, and `Prefer: outlook.body-content-type="text"`; `/me/messages/{id}/createReply` + PATCH for drafts. Handle 429/`Retry-After`, paging (`@odata.nextLink`), and errors (clear hints for consent/admin-approval errors like AADSTS65001/90094).
- **FolderSource** (no-IT fallback): watches `ATLAS_MAIL_FOLDER`, for example a OneDrive folder filled by a Power Automate flow or by an Outlook rule/manual save. It reads `.eml` (stdlib `email`), `.msg` (`extract-msg`) and `.json` (the Power Automate "When a new email arrives (V3)" output: `From, To, Cc, Subject, Body, DateTimeReceived, Id, ConversationId, WebLink`). `is_from_me` comes from `ATLAS_MAIL_ME` (the user's addresses, separated by `;`). `create_outlook_draft` returns None.
- Tests: FolderSource with fixture files of all three formats; GraphSource against a fake HTTP transport (httpx `MockTransport`), covering paging, 429, the consent-error hint, and the device-code flow with a fake MSAL app.
- `docs/INBOX_SETUP.md`: step-by-step for the user, (a) the Entra app registration settings (public client, redirect not needed, allow public client flows = yes, API permissions Mail.Read + Mail.ReadWrite delegated), and (b) the Power Automate flow (trigger "When a new email arrives (V3)" → "Create file" in OneDrive `/ATLAS-inbox/{Id}.json` with the listed fields; the same for Sent Items).

## 2. Inbox engine: `atlas/inbox/` (builder M2)

- **Agent HERMES** (`agents/corporate/hermes.yaml`, id `hermes`, nodes `[corporate]`, a role prompt about extracting commitments precisely, with no speculation and dates normalized to the user's timezone `America/Mexico_City`).
- **Scan** = a live **mission** with objective "Inbox scan · <date time>", so it shows in the Command Center like any mission, with phases, evidence and a mission report. The steps:
  1. `source.list_messages(since=last_scan or now-3d)`, skipping message ids already processed (`atlas-local/inbox/state.json`).
  2. In batches of ≤ 10 messages, HERMES gets the header + trimmed body and calls `record_followups {items: [{message_id, kind, title, detail, counterpart, due?, priority, excerpt}]}` (or none). Each processed email records evidence `email_read`.
  3. **De-duplicate** against open follow-ups (same counterpart + similar title, or same conversation) by updating instead of creating.
  4. **Staleness pass** (no LLM): a THEIR_COMMITMENT past due, or an AWAITING_REPLY with no reply in the conversation after `ATLAS_FOLLOWUP_DAYS` (default 3) business days, is marked WAITING and queued for a draft.
  5. **Drafts**: ALFRED calls `draft_email {followup_id, to, cc, subject, body}` for the queued items. Language and tone follow the thread (usually Spanish, professional, short). Evidence `draft_created`. Draft status PROPOSED.
  6. The mission report lists the counts (emails read, follow-ups new/updated, drafts proposed) and anything unreadable.
- **Scheduler**: `ATLAS_INBOX_SCHEDULE` = local times, e.g. `08:00,15:00` (weekdays only by default). It only runs while ATLAS is running, and runs one missed scan at startup if the last scan is older than the previous slot. `POST /inbox/scan` runs a scan now.
- **Drafts decision**: `POST /drafts/{id}/decision {decision: "APPROVED"|"DISCARDED", subject?, body?, to?, cc?}`. When approved, the source creates an Outlook draft if it can (`export="outlook_drafts"`, `download_url` = the web link), otherwise ATLAS writes `atlas-local/outputs/corporate/inbox/<id>.eml` with header `X-Unsent: 1` (`export="eml"`, `download_url=/drafts/{id}/eml`).
- **Follow-ups API**: `GET /followups?status=&kind=` and `PATCH /followups/{id} {status?, due?, title?, priority?}`. `POST /followups/{id}/draft` asks ALFRED to draft now (a one-shot, not a full mission).
- **Status**: `GET /inbox/status` returns `{source, connected, account, last_scan, next_scan, schedule, processed_count, hint}`.
- Everything goes through `WorldStore` (`upsert_followup`, `upsert_draft`, which emit the events) and is persisted by the existing event log.
- Works on both LLM backends, using `executor.structured` for `record_followups`/`draft_email` (see how create_plan is run).

## 3. UI (builder U)

A **Follow-ups** panel (new; a tab or panel in the Command Center):
- Grouped columns: *Mine to do* (MY_COMMITMENT + REQUEST_TO_ME), *Waiting on others* (THEIR_COMMITMENT), *Replies owed / no answer* (AWAITING_REPLY).
- Each card shows the title, counterpart, due date (overdue in red), priority, the source email (subject · sender · date, excerpt on hover, "Open in Outlook" when there's a web link), and the actions *Done*, *Dismiss*, *Snooze* (sets a due date) and *Draft follow-up*.
- A **Draft review** drawer/modal shows the editable to/cc/subject/body, with Approve and Discard. After approval it shows "Open in Outlook" (outlook_drafts) or "Download .eml" (eml).
- An **Inbox status** chip in the header shows: not connected (click → connect flow with the device code shown large, plus the link to microsoft.com/devicelogin), connected as …, last scan, next scan, and a "Scan now" button.
- The activity feed shows `followup.upserted` / `draft.upserted` tagged `FUP` / `DRF`.
