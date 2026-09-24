# ARGOS: continuous monitoring (Phase 4)

ARGOS watches the operation and raises **alerts**, each backed by verbatim evidence. It keeps **Rock** statuses current
using the user's own rules, and prepares the **Monday L10 brief**. Contracts are in `core/models.py`: `Alert`, `AlertEvidence`,
`RockStatus`, `Brief`. The events `alert.upserted` (`{alert}`), `rock.updated` (`{rock}`) and `brief.ready` (`{brief}`) populate
`WorldState.alerts/rocks/briefs`. The reducers are generic.

All private material lives under `<ATLAS_LOCAL_DIR>/argos/` and is never committed:
```
argos/
  dashboards/<project>/<iso-week>/<file>     attachments pulled from email (company files, kept for week-over-week diffs)
  snapshots/<project>/<iso-week>.json        normalized extraction of that week's dashboard
  rocks.yaml                                 the user's Rocks (a commented example is written if missing)
  watch.yaml                                 dashboard rules + L10 thresholds (example written if missing)
  state.json                                 processed message ids, last runs
```

## Framework (builder A1: `atlas/argos/{engine,scheduler,checks}.py`, `routes/argos.py`, store methods)

```python
@dataclass
class AlertDraft:            # what a check returns; the engine turns it into an Alert
    check: str; kind: str; severity: str; title: str; detail: str
    project: str | None; evidence: list[AlertEvidence]; fingerprint: str

class Check(Protocol):
    name: str                                   # "dashboards" | "l10" | "rocks"
    async def run(self, ctx: CheckContext) -> CheckResult   # alerts: list[AlertDraft], notes: list[str]
```
`CheckContext` holds: the store, the mission scope (for LLM steps through `executor.structured` and evidence), the
mail source (`atlas.inbox.sources.make_source()`), `paths`, the loaded `watch.yaml`, and `now`.

- **A run is a mission**, "ARGOS watch · <checks> · <time>", in the corporate node. Each check is a task assigned to argos. The mission report lists new, updated and resolved alerts per check, plus notes and failures. A check that fails (for example, the Suite is unreachable) marks its task FAILED and the others continue.
- **Upsert by fingerprint:**
  - A draft whose fingerprint matches an OPEN or ACKNOWLEDGED alert updates it (`last_seen`, evidence).
  - A new fingerprint creates an alert.
  - When a check completes successfully, OPEN alerts of that check that weren't seen get **RESOLVED** automatically, with the summary "Resolved · <title> (no longer detected)".
  - ACKNOWLEDGED alerts are left alone.
- **Scheduler** (env): `ATLAS_ARGOS_SCHEDULE` defaults to `"09:30,16:00"` (weekdays, America/Mexico_City). `ATLAS_ARGOS_BRIEF` defaults to `"MON 07:30"`. Setting either empty disables it. Catch-up at startup works like the inbox scheduler. Only one run at a time, with a shared lock that also serializes it with inbox scans. It must stop fast on Ctrl+C.
- **Agent state:** while idle, ARGOS shows `MONITORING` with the activity "Watching dashboards, L10, Rocks · next check 16:00".
- **API:**
  - `GET /argos/status` → `{checks: [{name, enabled, last_run, last_ok, note}], next_run, next_brief, running, mission_id}`
  - `POST /argos/run {checks?: [..]}` → Mission (409 if running or if a check lacks configuration, with a hint)
  - `GET /alerts?status=&check=&project=`
  - `PATCH /alerts/{id} {status}`
  - `GET /rocks`
  - `POST /rocks/reload`
  - `PATCH /rocks/{id} {current}` (a manual progress update, which writes `rocks.yaml` back while preserving its comments; use ruamel.yaml if needed)
  - `POST /argos/brief` → Mission (build the brief now)
  - `GET /briefs`
  - `GET /briefs/{id}/file`

## Dashboards check (builder A2: `atlas/argos/dashboards.py` + attachment support in `atlas/inbox/sources/*`)

- **Attachment support:** add `list_attachments(message_id) -> list[AttachmentMeta{id,name,size,content_type}]` and `download_attachment(message_id, attachment_id) -> bytes` to `MailSource`, implemented for Graph (`/me/messages/{id}/attachments`, file attachments only, skipping inline images) and for the folder source (`.eml`/`.msg` attachments; for `.json`, from sibling files the flow saved, if any). Also add a `has_attachments: bool` field on `MailMessage` (Graph `hasAttachments`).
- **Finding dashboards:** messages from the last `lookback_days` (default 10) with attachments whose subject or file name matches `watch.yaml` → `dashboards.match` (defaults: `semana`, `reporte semanal`, `dashboard`, `S\d{2}` as a regex, `.pdf`/`.xlsx`/`.xlsm`). The project comes from the file name or subject using the **same project keywords as `inbox/digest_rules.yaml`**, overridable in `watch.yaml`. The ISO week comes from the email date, or a week token like `S38`/`Semana 38` when present. Files are saved to `dashboards/<project>/<week>/` along with their sha256 hash; already-downloaded attachments are skipped.
- **Snapshot:** extract with `atlas.live.files.extract_text` (pdfplumber tables, openpyxl sheets), then normalize into `{kpis: {label: value}, rows: [...], dates: [{label, date}], text_hash}`. This is deterministic, best-effort parsing of "label: value" lines and table rows.
- **Deterministic findings** (no LLM):
  - `missing_report`: an expected project (`watch.yaml` → `dashboards.expected`, or else any project seen in the previous 3 weeks) has no file this week.
  - `identical_report`: the same sha256, or the same `text_hash`, as the previous week.
  - `kpi_mismatch`: best-effort, for a KPI like "escrituradas 22/51" where a table lists a different count of rows with that status. Only when confident.
- **ARGOS analysis** (LLM, `executor.structured`, one call per project with a previous week): the previous and current extracted text (truncated sensibly), the deterministic findings and the project context. The tool is `record_dashboard_findings {findings: [{kind: moved_date|removed_row|value_change|other, severity, title, detail, evidence: [{version, quote}]}]}`. **Every quote must appear verbatim** in the named version, otherwise it's dropped, and a finding left with no evidence is dropped too. The fingerprint is `dashboards:<project>:<kind>:<normalized title>`.
- Evidence records `file_read` for each dashboard read.
- **Privacy:** the dashboards are the user's own work files, stored only locally. Email bodies are still not stored.

## L10 and Rocks checks + weekly brief (builder A3: `atlas/argos/{suite,l10,rocks,brief}.py`)

**PAGA Suite client** (`suite.py`): `ATLAS_SUITE_URL` (e.g. `https://pagasuite.com/api`), `ATLAS_SUITE_TOKEN` (a Bearer token), and optional `ATLAS_SUITE_AUTH_HEADER` (default `Authorization`). Endpoints (per the dossier): `GET /l10/admin/todos`, `GET /l10/issues`, `GET /l10/resumen/{semana_id}`. It must be defensive about field names (log unknown shapes and map aliases: `responsable`, `fecha`, `avance_pct`, `semaforo`, `estado`/`status`, `decide`, `abierto_desde`/`created_at`), with timeouts and clear errors. Without configuration the check is "not configured" (its status carries a hint), not an error.

**L10 check** (`l10.py`, deterministic, with no LLM needed):
- `overdue_todo`: past its date and not done.
- `unreported_todo`: no report for the current open week.
- `stale_issue`: open for more than `watch.yaml` → `l10.stale_issue_days` (default 14), or with no decider.

Severity depends on the age. Fingerprints are `l10:<kind>:<id>`. The rule "don't report on behalf of others" applies: ATLAS only reads and never writes to the Suite.

**Rocks** (`rocks.py`): `rocks.yaml` holds the quarter and a list of Rocks, each with `{id, title, owner, project?, due, metric?, target?, current?, start_value?, start_date?, done?: bool}`. The rules, straight from the user's dossier:
- **Overdue and not done → FAILED** (alert `rock_failed`, HIGH).
- **Observed pace < 50% of required pace → AT_RISK** (alert `rock_at_risk`). Observed pace = (current − start_value) / weeks elapsed. Required pace = (target − current) / weeks left.
- If there's no metric or target, the status is UNKNOWN, with reason "needs a measurable". Missing measurables are themselves a finding in EOS terms, so these get an `other` alert, LOW.
- A Rock marked done → DONE.

Each Rock emits `rock.updated` with the computed statuses and paces. A Rock with two owners (a `·`, `/` or `,` in `owner`) gets an `other` alert: "Rock with two owners has no owner". The dossier found that none of the 7 shared-owner Rocks closed.

**Weekly brief** (`brief.py`): builds a `Brief` for the ISO week. The headline has ≤ 3 lines. The sections are: *Rocks* ("X of N on-track", then the ones at risk or failed with their reasons), *Dashboards* (the week's open alerts by project), *L10* (overdue and unreported to-dos, stale issues) and *Follow-ups* (the user's overdue items from the inbox board). The prose comes from ARGOS through a structured step, but **every number is computed by code** and passed in. It writes `L10 brief <week>.docx` using `live/files.py`'s docx writer into `outputs/corporate/argos/`, and `GET /briefs/{id}/file` serves it.

## UI (builder U)

A **Monitor** view, a third top-level view next to Missions and Follow-ups (key `3`):
- **Alerts:** grouped by project, then by check, with severity, title, detail and the evidence quotes. Previous and current quotes are shown side by side when both exist. The actions are *Acknowledge* and *Resolve*. Filters: open, acknowledged, resolved.
- **Rocks:** a table with status chips (ON_TRACK green, AT_RISK amber, OFF_TRACK/FAILED red, UNKNOWN grey, DONE white), owner, due date, metric with current/target and a progress bar, required vs observed pace, and the reason. There's an inline "update current" editor (a PATCH), and "X of N on-track" in the header.
- **Brief:** the latest brief's headline and sections, "Download .docx", a list of past briefs and "Build now".
- **Status strip:** each check's last run, ok or failed, and its note/hint (e.g. "PAGA Suite not configured: set ATLAS_SUITE_URL and ATLAS_SUITE_TOKEN"), the next run, and "Run checks now".

The activity feed tags `alert.upserted` as `ALR`, `rock.updated` as `RCK` and `brief.ready` as `BRF`. Open HIGH alerts show a red count on the Monitor tab.
