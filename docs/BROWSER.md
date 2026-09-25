# Agent browser

Some sources have no API: a market-data platform you pay for and use while signed in. An agent with a
`browser:` block can work those sites the way you do. It opens pages, reads them, sets filters, extracts tables
and downloads the exports.

It **never uses your everyday Chrome**. Each such agent has its own dedicated browser profile, in
`<ATLAS_LOCAL_DIR>/browser/<profile>/`. You sign into the site in that profile once, and it stays signed in until
the site logs it out. Claude in Chrome stays switched off for every ATLAS agent (`--no-chrome`).

## Setup (MERCATO)

1. In `.env`, set the site:

   ```
   ATLAS_MERCATO_BROWSER_URL=https://app.the-site.com/studies
   ATLAS_MERCATO_BROWSER_DOMAINS=the-site.com
   ```

   `DOMAINS` lists the only sites the agent may open. Subdomains are included and entries are comma-separated.
   Add the site's login or SSO domain only if the pages themselves live there.

2. `cd apps/api && uv sync`. This installs Playwright. It drives your installed **Google Chrome**, so there is no
   browser to download. To use Edge instead, set `ATLAS_BROWSER_CHANNEL=msedge`.

3. Sign in once:

   ```
   uv run atlas-browser login market-studies
   ```

   A Chrome window opens on the start page. Sign in and tick "remember me" if the site offers it, then **close
   the window**. The session is now saved in the profile.

4. Check it: `uv run atlas-browser status`.

Do this on the machine that runs ATLAS. The profile is local, just like the Outlook token cache. If the site logs
the session out, the agent stops and reports that it needs a login. Run step 3 again.

## What the agent can do

| Tool | What it does |
| --- | --- |
| `browser_open` | Opens a page on the allowed sites. With no URL it opens the start page. |
| `browser_snapshot` | Returns the URL, title and visible text (paged), plus the interactive elements with refs (`e12`; `f1e3` inside frames). |
| `browser_click` / `browser_type` / `browser_select` / `browser_press` / `browser_scroll` / `browser_back` | Operate the page by ref. |
| `browser_wait` | Waits for a report that takes time to build, or for a given text to appear. |
| `browser_tables` | Extracts the visible tables and grids. With `save_as`, saves them to the mission outputs as xlsx (one sheet per table) or csv. |
| `browser_download` | Clicks an export button and saves the file it produces to the mission outputs. |

Every file lands in `outputs/<node>/<mission>/`. It appears as a deliverable in the report, and the agent reads it
with `read_file`. Files that a normal click happens to download are saved the same way.

## Guard rails

These are enforced in `atlas/live/browser.py`, not just asked of the model.

- **Only `allowed_domains`.** Opening any other site is refused. If a click lands elsewhere, the agent goes back
  (or the new tab closes) and the step is recorded as blocked.
- **No passwords.** The agent never types into a password field. Signing in is your job.
- **Consequential clicks need you.** This covers buttons and links whose text reads as delete, buy, pay,
  subscribe, send, publish, share, log out or credits / créditos (Spanish or English). The agent has to call
  `request_approval` and wait for your OK, then click again with `confirm: true`. Watch for platforms that charge
  credits per report: the approval card shows exactly which click is being approved.
- **One task per profile at a time.** Other tasks wait (`ATLAS_BROWSER_LOCK_WAIT`, 900 s by default).
- **Evidence.** Each page opened (`browser_visit`), action taken (`browser_action`, e.g. `click "Exportar"`) and
  file saved (`browser_download`, `file_written`) is recorded by the system. AUDITOR and the claim check can
  therefore verify "exported comparables.csv" the same way they verify files that were read.
- Browser tasks get their own turn budget (`browser.max_turns`, 40 by default), because a site flow takes many
  steps.

By default the window is visible (`ATLAS_BROWSER_HEADLESS=0`). Many sites block headless browsers, and a visible
window lets you watch the agent work. On an always-on PC the window simply opens and closes with each task.

Before automating a site, check its terms of use: some data platforms forbid automated access. If the site has an
API or scheduled exports, use those instead.

## Giving another agent a browser

```yaml
browser:
  profile: my-agent            # folder name under atlas-local/browser/
  start_url: ${MY_SITE_URL}
  allowed_domains: ["${MY_SITE_DOMAINS}"]
  max_turns: 40
```

Then run `uv run atlas-browser login <agent-id>`.
