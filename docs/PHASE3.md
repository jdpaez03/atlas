# Phase 3: files, evidence, mission thread, history

This phase comes from the first real use of ATLAS.
1. Agents couldn't read files the user pointed them to (they had no file tools), and they *described* actions they never performed.
2. The user couldn't follow up inside a mission.
3. Missions disappeared when the server restarted.

**The principle: every action is recorded by the system, never by the agent.** Reports and the activity feed are built from those records, so anything they claim can be checked.

Contracts (already in `core/models.py`): `Evidence`, `AgentReport.evidence/deliverables`,
`MissionReport.version/deliverables`, `Mission.attachments/round/interrupted`, `Task.round`,
`Attachment.size_bytes/download_url`, `HUMAN = "human"`, event `evidence.recorded` (`{evidence}`),
`WorldState.evidence`. Shared helpers: `core/paths.py` (local dir, db, attachments, outputs, `safe_name`,
`is_within`) and `WorldStore.record_evidence()` / `evidence_for()`.

---

## A. Files and evidence (engine builder F)

**Read roots, per node:** `ATLAS_FILE_ROOTS_<NODE>` (e.g. `ATLAS_FILE_ROOTS_CORPORATE`), with paths separated by `;`.
The defaults are `corporate` → `~/Documents` and every other node → none. The attachments folder of the
current mission is always readable too. A node never reads another node's roots.

**Safety** (a single function, tested):
- Resolve real paths and follow symlinks; the result must be inside an allowed root.
- Always deny: `.git`, `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.ssh`, `~/.claude`, `atlas-local/context/<other node>`, `atlas-local/atlas.db`, and the `atlas-local/agents` folder.
- Read-only on the user's files: 25 MB per file max, text truncated to `ATLAS_FILE_MAX_CHARS` (default 60k), with `offset` paging.

**Tools** (both backends):
| Tool | What it does |
|---|---|
| `list_files(path, pattern?, recursive?)` | ≤ 500 entries: name, size, modified date |
| `search_files(query, under?)` | find by name under the allowed roots (≤ 100 hits) |
| `read_file(path, offset?, max_chars?, sheet?)` | text extraction: txt/md/csv/json/code; PDF (pypdf); xlsx/xlsm (openpyxl, cell values, one block per sheet with the sheet name; `sheet` selects one); docx (paragraphs + tables); pptx (slide text) |
| `write_deliverable(filename, format, content? , sheets?)` | formats `md`, `txt`, `csv`, `json`, `xlsx` (sheets: `{name: rows[][]}`), `docx` (markdown-ish content → headings/paragraphs/bullets/tables). Written **only** to `outputs/<node>/<mission_id>/`, never overwriting (`name (2).ext`). Returns an `Attachment` with `download_url = /missions/{id}/files/outputs/{name}` |

**Evidence:** every call to one of these tools, plus consult, approval, web search and web fetch, records an `Evidence` item
through `store.record_evidence` (with `ok=false` and the reason on failure). The activity line an agent shows comes
**only** from real tool calls ("Reading Rocks_Q3.xlsx", "Writing memo.docx"), never from model text.

**Reports:** on `submit_report`, the runtime attaches the task's evidence and deliverables to the `AgentReport`. It also runs
a claim check: when `actions_taken` or `inputs_used` mentions a file name or path that isn't in the evidence, it adds
the limitation `"Unverified: <item> (no system record)"` and drops the report's confidence to LOW. The mission report
aggregates every deliverable.

**Prompts:** the protocol states the file tools, the read-only rule and the evidence rule ("the system attaches a log of
what you actually did; never claim an action you didn't take through a tool").

**Subscription backend:** the same tools as in-process MCP tools. Claude Code's built-in Read/Glob/Grep stay **disabled**:
the only file access is our sandbox.

---

## B. History and mission thread (engine builder P)

**Persistence:** SQLite at `paths.db_path()`. Every published event is appended as a row (seq, type, mission_id, json).
On startup, events are replayed into the `WorldStore` (fold), and the bus history is seeded so WS replay works.
Missions that weren't CLOSED at shutdown get `interrupted=true`, their open tasks go CANCELLED and pending
approvals go EXPIRED. This is emitted as normal events with a log line. `/reset` also clears the database.

**History API:** `GET /missions?node=&limit=` returns the newest first, `{id, objective, node, mode, phase, round, interrupted, created_at, closed_at, report_versions, usage}`.

**Attachments:**
- `POST /missions` also accepts `multipart/form-data` (`objective`, `node`, `mode`, `scenario_id?`, `files[]`).
- `POST /missions/{id}/attachments` accepts `files[]`.
- Files are stored in `paths.attachments_dir(id)`, limited to 25 MB each and 20 per mission, and added to `Mission.attachments` via a `mission.updated` event.
- `GET /missions/{id}/files/{attachments|outputs}/{name}` downloads a file, with path-safe checks.

**Thread:** `POST /missions/{id}/messages {text}` records an `AgentMessage` from `human` to `atlas` (type REQUEST).
- **Mission running (live):** the note is added to the mission's "human guidance". Every task that starts afterwards gets it in its prompt, and so do ATLAS's review and consolidation steps. ATLAS acknowledges it with a short ANSWER message on the fast model.
- **Mission closed (live):** a follow-up round. ATLAS gets the objective, the latest mission report, the agent report summaries, the thread and the attachments, and calls `respond_to_followup {answer?, tasks?[]}`.
  - Answer only: an ANSWER message from atlas to human.
  - Tasks: `round += 1`, the phase goes back through DELEGATION → EXECUTION → … → CLOSED, and the tasks carry `round=N`. The dependencies can reference earlier tasks' reports. A new `MissionReport` with `version=N` is added (older versions stay).
- An interrupted mission can be resumed the same way.
- **Simulated missions:** ATLAS replies "This is a simulated mission; follow-ups need a live mission."

---

## C. UI (builder U)

- Adapt to the new types (store, mock).
- New-mission form: attach files (drag & drop, several at once) and send them as multipart.
- **Mission thread** panel: human ↔ ATLAS chat. The input is available for live missions (running or closed). Show round dividers ("Round 2 started").
- Reports:
  - An **Evidence** section per agent report: an icon per kind, the path/URL/agent, ok or failed, and the time.
  - Unverified claims highlighted.
  - **Deliverables**, with download links, both per agent and at mission level.
  - A mission report version selector (v1, v2…).
- Activity feed: `evidence.recorded` events tagged `EVD`.
- **Mission history** drawer: past missions from `GET /missions`, with an interrupted badge. Selecting one shows it (the state already has every mission).
