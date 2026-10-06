# Agents and PAGA Suite (Level 10)

Builder: `apps/api/atlas/live/suitetools.py`. In the Suite (paga-app): `api/main.py` `_atlas_puede`, and `importar_todos` in `api/routers/l10.py`.

## What agents can do

| Tool | What it does | Approval |
|---|---|---|
| `suite_read(what)` | Reads one of: `todos` (estado `vivos` = open), `issues` (`abiertos`), `junta` (the meeting view of a week), `contexto` (the current week: id, clave, abierta/cerrada), `resumen` (needs `semana_id`), `rocks`. | none |
| `suite_add_todos(todos, fuente)` | Adds **new** to-dos (`POST /l10/admin/importar`, origin `junta`). Each row has `titulo`, plus optional `responsable`, `fecha_compromiso`, `proyecto`, `contexto`, `decide` and `involucrados`. | inside the tool |
| `suite_report_todos(semana_id, fuente, reportes)` | Captures, in the **open** week, the status of the to-dos the meeting talked about (`PUT /l10/reportes/{id}` per row): `estatus`, `avance_pct`, `requiere_apoyo` / `apoyo_area`, and a required `comentario` with what was said and the minute. The approval shows each row's before → after. The comment is appended to the person's own. It's recorded as captured by ATLAS (`reportado_por`), so it never counts as a self-report. | inside the tool |
| `suite_open_week(fecha_junta)` | Opens the L10 week of that meeting (`POST /l10/admin/semanas`). | inside the tool |
| `suite_close_week(semana_id, resumen)` | Closes the week (`POST /l10/admin/semanas/{id}/cerrar`), which freezes its reports. | inside the tool |

**How a write is approved:**
- The approval is part of the write tool. The human sees the exact rows in Approvals, and on approval the tool sends exactly those rows.
- A rejection writes nothing. The note goes back to the agent.
- The agent never calls `request_approval` for these tools, and the planner doesn't add a separate approval task.

**Evidence:** every call is recorded as `external_call` (`PAGA Suite GET …` / `POST …` / `PUT …`). The report's claim check and the AUDITOR see them.

## What the Suite still never allows ATLAS

ATLAS can't do any of these:
- edit, cancel or close an existing to-do;
- report progress in a closed (frozen) week, report without a comment, or assign help to a person (that sends an email);
- confirm proposed owners;
- touch issues;
- reach anything outside `/l10`.

These limits are enforced by the Suite itself:
- The key's allowlist answers 403 to everything else.
- `reportar_avance` refuses ATLAS captures with `apoyo_email` (403) or without `comentario` (400); a closed week is 403 for everyone.
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

> Revisa la junta del lunes: captura el avance de los to-dos, cierra la semana y da de alta los nuevos.

The Monday meeting reviews the week that is still **open** (opened at the previous meeting). So the order matters:

1. Read the meeting's minuta (`transcripts/<date> Junta Semanal.minuta.md`): its *Seguimiento* section lists what was said about existing to-dos, its *To-dos* table the new commitments.
2. Run `suite_read junta` for the open week: codes, current reports.
3. Call `suite_report_todos` with the to-dos the meeting discussed. You approve the before → after.
4. Call `suite_close_week` with the summary. You approve. The week freezes.
5. Call `suite_open_week` for the meeting's date (if not open yet). You approve.
6. Run `suite_read todos vivos`, then call `suite_add_todos` with only the new ones. You approve.

If no week is open, new to-dos exist in the Suite but get their report rows when the next week is opened (`suite_open_week`).
