# AUDITOR and error recovery (Phase 5)

## AUDITOR — the quality gate

AUDITOR (`agents/auditor.yaml`, `plannable: false`) never gets tasks from the planner. After the tasks of a round
finish (and after ATLAS's own review/follow-ups), it checks **every agent report of that round** before
consolidation.

1. **System checks first** (`atlas/live/auditor.py` `precheck`, no LLM). For each report:
   - how many FACT findings cite no source;
   - file/action mentions with no system record (the Phase 3 claim check);
   - output the agent never structured (auto-wrapped);
   - facts with no recorded action at all (they rest only on context and inputs);
   - external agents (their actions are self-reported);
   - recorded actions that failed.
   Some of these set a floor: an auto-wrapped or unverified report can never get PASS.
2. **AUDITOR's step** (one structured call, `submit_audit`). It reads each report next to the evidence ATLAS
   recorded for its task and returns a verdict:
   - `PASS`: it holds up.
   - `ISSUES`: it's usable, with the listed caveats.
   - `FAIL`: a key finding is unsupported, mislabeled in a way that changes the decision, or wrong.

   Each issue quotes the finding and states the problem. Its kind is one of: unsupported, mislabeled,
   inconsistent, calculation, stale, scope or other. Its severity is LOW, MEDIUM or HIGH.
3. **One revision round.** Each FAIL opens `Revise: <task>` for the same agent (`Task.revision_of`). The task
   depends on the original, so it sees its own report, and the issues are written as instructions. The revision
   is audited again as `final`, and a failed revision is not sent back a second time.
4. **Consolidation** receives each report's AUDIT line and an instruction: build on what held up, use
   revisions, and never present an unsupported or mislabeled finding as a FACT.
5. **Every figure must trace back.** After consolidation, the system extracts the figures in the executive
   summary and key findings. Numbers with a unit or ≥ 10 count; years and small counts are skipped; a range is
   one figure. Each must appear in some agent report, the evidence, the objective or the human's own words.
   Anything else goes to `MissionReport.untraced`, shown in the UI as "Figures not traced to any agent report".
   `MissionReport.audit_summary` says what was checked and what came of it.

AUDITOR never adds findings, research or recommendations. If its step fails (API error, usage limit), the
mission still closes, and the report says `Audit skipped · <why>`.

Events: `audit.recorded` (payload `{audit}`). The records are `WorldState.audits`.

Settings (`.env`):

| Variable | Default | Meaning |
|---|---|---|
| `ATLAS_AUDIT` | on | `off` disables AUDITOR (missions run as before Phase 5) |
| `ATLAS_AUDIT_REVISIONS` | 1 | `0` = audit only, never send a report back |

## Retries and resume

- **Task retries.** A task that fails with a transient error is re-run automatically: an API/LLM error, a
  network error, a timeout, or an external agent that didn't answer. `Task.retries` counts the re-runs and the
  activity feed says why. The plan's usage limit is never retried, and neither is a programming error.
  - `ATLAS_TASK_RETRIES` (default 1) sets how many re-runs.
  - `ATLAS_TASK_RETRY_DELAY` (seconds, default 10) sets the first wait; it doubles on each re-run.
- **Resume.** `POST /missions/{id}/resume` (the **Resume** button) takes a closed live mission that has FAILED or
  CANCELLED tasks, including one interrupted by a restart, and runs those tasks again as round N+1:
  1. The tasks are reopened on their dependencies.
  2. Completed work is not redone.
  3. The new reports are audited.
  4. The mission report is written as version N+1.

  The endpoint answers 409 when the mission is running or there's nothing to resume.

## Usage by agent

The Meter attributes every LLM call to the agent whose work it is, through a context variable set around each
task, ATLAS step and audit. Consults count toward the agent that asked. ARGOS and HERMES runs count toward those
two agents. The result is `Mission.usage_by_agent`, which sums to `Mission.usage`. `GET /usage?days=30&node=`
aggregates live missions by agent and by day. On the Claude plan, dollar figures are API-equivalent estimates,
not charges.
