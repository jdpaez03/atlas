# Agents and PAGA Suite (Level 10)

Builder: `apps/api/atlas/live/suitetools.py`. In the Suite (paga-app): `api/main.py` `_atlas_puede`, and `importar_todos` in `api/routers/l10.py`.

## What agents can do

| Tool | What it does | Approval |
|---|---|---|
| `suite_read(what)` | Reads one of: `todos` (estado `vivos` = open), `issues` (`abiertos`), `junta` (the meeting view of a week), `contexto` (the current week: id, clave, abierta/cerrada), `resumen` (needs `semana_id`), `rocks`. | none |
| `suite_add_todos(todos, fuente)` | Adds **new** to-dos (`POST /l10/admin/importar`, origin `junta`). Each row has `titulo`, plus optional `responsable`, `fecha_compromiso`, `proyecto`, `contexto`, `decide` and `involucrados`. | inside the tool |
| `suite_open_week(fecha_junta)` | Opens the L10 week of that meeting (`POST /l10/admin/semanas`). | inside the tool |
| `suite_close_week(semana_id, resumen)` | Closes the week (`POST /l10/admin/semanas/{id}/cerrar`), which freezes its reports. | inside the tool |

**How a write is approved:**
- The approval is part of the write tool. The human sees the exact rows in Approvals, and on approval the tool sends exactly those rows.
- A rejection writes nothing. The note goes back to the agent.
- The agent never calls `request_approval` for these tools, and the planner doesn't add a separate approval task.

**Evidence:** every call is recorded as `external_call` (`PAGA Suite GET …` / `POST …`). The report's claim check and the AUDITOR see them.

## What the Suite still never allows ATLAS

ATLAS can't do any of these:
- edit, cancel or close an existing to-do;
- report progress on anyone's behalf;
- confirm proposed owners;
- touch issues;
- reach anything outside `/l10`.

These limits are enforced by the Suite itself:
- The key's allowlist answers 403 to everything else.
- `importar` refuses ATLAS rows that carry a `codigo`, `csv` or `semana_apertura`, so it can never overwrite an existing to-do or create a week as a side effect.

## Setup

**In the Suite** (`docker/.env` of paga-app):
```
ATLAS_API_KEY_WRITE=1
```
Then restart the api (`docker compose up -d api`; never `down -v`).

**In ATLAS** (the server's `~/atlas/.env`):
```
ATLAS_SUITE_WRITE=1
```
Then `sudo systemctl restart atlas-api`.

Notes:
- Reads only need the existing Suite connection (`ATLAS_SUITE_URL` + `ATLAS_SUITE_TOKEN`).
- Without `ATLAS_SUITE_WRITE`, agents only get `suite_read`.
- Without the Suite flag, a write fails with a 403 that names the missing flag.

## Typical mission

> Close the week and add the to-dos from Monday's weekly meeting.

1. Read the transcript (`transcripts/…`).
2. Run `suite_read todos vivos` so nothing already in the Suite is added again.
3. Call `suite_add_todos`. You approve the rows.
4. Run `suite_read junta` to check who reported.
5. Call `suite_close_week` with that summary. You approve.

If no week is open, new to-dos exist in the Suite but get their report rows when the next week is opened (`suite_open_week`).
