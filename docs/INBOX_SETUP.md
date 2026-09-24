# Inbox setup: connect your work mailbox

ATLAS reads your Microsoft 365 / Outlook work mail to find follow-ups (see `docs/INBOX.md`). **It never sends
email.** It only reads your mail, and, if you allow it, saves drafts into your Outlook **Drafts** folder for you to
review and send yourself.

You have two ways to connect. Pick one:

| | A. Microsoft Graph (recommended) | B. Power Automate folder (no IT needed) |
|---|---|---|
| Needs | An app registration in Microsoft Entra (you, or IT) | Power Automate + OneDrive (included in most M365 plans) |
| Reads | Inbox + Sent Items, live | Whatever the flow copies into a OneDrive folder |
| Drafts in Outlook | Yes (with Mail.ReadWrite) | No: approved drafts download as `.eml` files that open in Outlook |
| "Open in Outlook" links | Yes | No |

If option A fails with *"Your organization requires admin approval for this app"*, either ask IT to approve the app
(send them section A) or use option B.

---

## A. Microsoft Graph

### A1. Register the app (5 minutes)

1. Go to **https://entra.microsoft.com** and sign in with your work account.
2. In the left menu open **Entra ID → App registrations** (older menus: **Identity → Applications → App
   registrations**). Click **+ New registration**.
   - **Name:** `ATLAS Inbox`
   - **Supported account types:** *Accounts in this organizational directory only (Single tenant)*
   - **Redirect URI:** leave it empty.
   - Click **Register**.
   > If you get "You do not have access" or the **New registration** button is disabled, your organization only lets
   > admins register apps. Ask IT to do steps 2–5 for you, or use option B.
3. On the app's **Overview** page, copy:
   - **Application (client) ID** → `ATLAS_MS_CLIENT_ID`
   - **Directory (tenant) ID** → `ATLAS_MS_TENANT_ID`
4. Open **Authentication**. Under **Advanced settings**, set **Allow public client flows** to **Yes**, then click
   **Save**. (In the newer Authentication page, this is on the **Settings** tab.) You don't need a redirect URI or a
   client secret, because ATLAS uses the *device code* sign-in.
5. Open **API permissions → + Add a permission → Microsoft Graph → Delegated permissions**. Tick
   **Mail.Read** and **Mail.ReadWrite**, then click **Add permissions**. Keep the default **User.Read**.
   - **Mail.ReadWrite** is only used to save drafts in Outlook. If you don't want that, skip it and set
     `ATLAS_MS_DRAFTS=0`.
   - If you are an admin, or IT is doing this, click **Grant admin consent for <your org>** so nobody sees a consent
     prompt.

### A2. Configure ATLAS

In the repo's `.env` file:

```ini
ATLAS_MAIL_SOURCE=graph            # or auto
ATLAS_MS_CLIENT_ID=<Application (client) ID>
ATLAS_MS_TENANT_ID=<Directory (tenant) ID>
ATLAS_MS_DRAFTS=1                  # 0 = read-only, never touches Outlook Drafts
```

Restart ATLAS.

### A3. Sign in (once)

1. In ATLAS, click the **Inbox** chip in the header and choose **Connect**. (API: `POST /inbox/connect`.)
2. ATLAS shows a code, for example `F7K2-QX9P`. Open **https://microsoft.com/devicelogin**, enter the code, and sign in
   with your work account within 15 minutes.
3. Accept the permissions prompt. The chip changes to *Connected as you@company.com*.

ATLAS stores the sign-in token at `atlas-local/inbox/msal_cache.bin`. On Windows the token is encrypted with your
Windows account (DPAPI). On macOS/Linux it is a plain file, so keep `atlas-local` private. To disconnect, delete that
file (IT can also remove the app's access for you).

If your organization allows **Mail.Read** but not **Mail.ReadWrite**, ATLAS asks you for a second code and then
connects **read-only**. The status says so, and approved drafts download as `.eml` files instead.

### A4. Troubleshooting

| Message / error | What to do |
|---|---|
| *Your organization requires admin approval for this app* (AADSTS65001, AADSTS90094, AADSTS50105) | Ask IT to click **Grant admin consent** on the app (A1 step 5), or to assign you to it. Or use option B. |
| AADSTS7000218 (*client_assertion or client_secret*) | A1 step 4: **Allow public client flows = Yes**. |
| AADSTS700016 (*application not found*) | Wrong `ATLAS_MS_CLIENT_ID`, or wrong `ATLAS_MS_TENANT_ID`. |
| *The sign-in code expired* | Click **Connect** again and enter the new code within 15 minutes. |
| Throttling (HTTP 429) | ATLAS waits and retries on its own. Nothing to do. |

---

## B. Power Automate → OneDrive folder (no IT needed)

A flow copies each new email as a `.json` file into a OneDrive folder, which syncs to your computer, and ATLAS reads
that folder. ATLAS **never modifies, moves or deletes** these files.

### B1. Flow for the Inbox

1. Go to **https://make.powerautomate.com** and sign in with your work account.
2. Click **+ Create → Automated cloud flow**.
   - **Flow name:** `ATLAS inbox`
   - **Choose your flow's trigger:** search for and select **When a new email arrives (V3)** (Office 365 Outlook).
   - Click **Create**.
3. Click the trigger and set these parameters (open **Advanced parameters / Show all** if they're hidden):
   - **Folder:** `Inbox`
   - **Include Attachments:** `No`
   - **Only with Attachments:** `No`
   - **Importance:** `Any`
4. Click **+ (New step / Add an action)**, search for **OneDrive for Business**, and choose **Create file**.
   - **Folder Path:** `/ATLAS-inbox` (create the folder in OneDrive first, or pick it with the folder icon).
   - **File Name:** click **fx** (Expression) and paste:
     ```
     concat(formatDateTime(triggerBody()?['receivedDateTime'], 'yyyyMMdd-HHmmss'), '-', guid(), '.json')
     ```
   - **File Content:** click **fx** (Expression) and paste:
     ```
     string(triggerBody())
     ```
     This writes the whole message the trigger received (`id`, `conversationId`, `internetMessageId`, `from`,
     `toRecipients`, `ccRecipients`, `subject`, `body`, `isHtml`, `receivedDateTime`, …) as JSON. ATLAS also accepts a
     hand-built JSON with the keys `Id, ConversationId, InternetMessageId, From, To, Cc, Subject, Body,
     DateTimeReceived, WebLink`.
5. Click **Save**. Send yourself a test email: within a few minutes a `.json` file should appear in `ATLAS-inbox`.

### B2. Flow for Sent Items

ATLAS needs your sent mail to know which emails still wait for an answer.

1. Open the `ATLAS inbox` flow. Click **… → Save As**, and name the copy `ATLAS sent`. Then open **My flows**, find
   `ATLAS sent`, and turn it **On**.
2. Edit it. In the trigger, set **Folder** to `Sent Items`. In **Create file**, set **Folder Path** to
   `/ATLAS-inbox/sent`.
3. Click **Save**.

Files in a subfolder named `sent`, `Sent Items` or `Enviados` always count as sent by you. Other files count as sent
by you when their sender is in `ATLAS_MAIL_ME`.

### B3. Configure ATLAS

1. Make sure the OneDrive desktop app syncs `ATLAS-inbox` to this computer, for example
   `C:\Users\<you>\OneDrive - <Company>\ATLAS-inbox`.
2. In `.env`:
   ```ini
   ATLAS_MAIL_SOURCE=folder           # or auto (with ATLAS_MS_CLIENT_ID empty)
   ATLAS_MAIL_FOLDER=C:/Users/<you>/OneDrive - <Company>/ATLAS-inbox
   ATLAS_MAIL_ME=you@company.com;you.alias@company.com
   ```
3. Restart ATLAS. The Inbox chip shows *Watching …/ATLAS-inbox (N mail files)*.

You can also drop files into the folder by hand. From Outlook you can drag a message into it (classic Outlook saves a
`.msg`) or use **File → Save As** to get an `.eml`. ATLAS reads `.json`, `.eml` and `.msg`.

**Privacy:** these files hold full email bodies in *your* OneDrive. ATLAS only reads them, and it only keeps a short
excerpt per follow-up. To limit what builds up, delete old files now and then. Or add a third flow using
**Recurrence** (weekly), then **List files in folder**, then **Delete file** for files older than 30 days.

---

## All settings

| Variable | Default | Meaning |
|---|---|---|
| `ATLAS_MAIL_SOURCE` | `auto` | `graph`, `folder` or `auto`: graph if `ATLAS_MS_CLIENT_ID` is set, else folder if `ATLAS_MAIL_FOLDER` is set, else off |
| `ATLAS_MS_CLIENT_ID` | — | Entra app **Application (client) ID** |
| `ATLAS_MS_TENANT_ID` | `organizations` | Entra **Directory (tenant) ID** (recommended) |
| `ATLAS_MS_DRAFTS` | `1` | Ask for Mail.ReadWrite to save approved drafts in Outlook Drafts. `0` = read-only |
| `ATLAS_MAIL_FOLDER` | — | Folder with `.json` / `.eml` / `.msg` files (option B) |
| `ATLAS_MAIL_ME` | — | Your addresses, separated by `;`, so ATLAS can tell which emails you sent |
| `ATLAS_MAIL_BODY_MAX_CHARS` | `6000` | Max characters of an email body sent to the agents, after trimming quoted history |
