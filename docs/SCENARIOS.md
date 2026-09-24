# Scenario format (simulated missions, Phase 1)

Scenarios live in `apps/api/atlas/sim/scenarios/*.yaml`. The simulator (`atlas/sim/runner.py`)
plays them through the real `WorldStore`, so the UI can't tell a simulated mission from a live one.

```yaml
id: real-estate-investment
node: corporate
title: Real-estate investment analysis
objective: Analyze whether this real-estate investment opportunity makes sense.
steps:
  - { wait: 1.0, do: phase, phase: DECOMPOSITION }
  - { wait: 0.5, do: task, ref: research, title: Market research, description: "...",
      assigned_to: sofia, priority: HIGH, depends_on: [] }
  - { wait: 0.8, do: agent, agent: sofia, status: WORKING, activity: "Pulling comparables", task: research }
  - { wait: 2.0, do: message, from: oracle, to: sofia, type: REQUEST, subject: "...", body: "...",
      task: analysis, confidence: MEDIUM, requires_response: true }
  - { wait: 1.0, do: task_update, ref: research, status: COMPLETED, progress: 1.0 }
  - { wait: 1.0, do: report, agent: sofia, task: research, asked_to: "...", actions_taken: [...],
      inputs_used: [...], findings: [{kind: FACT, statement: "...", confidence: HIGH, sources: [...]}],
      unresolved: [...], confidence: HIGH, limitations: [...] }
  - { wait: 1.0, do: approval, ref: send-package, requested_by: alfred, reason: EXTERNAL_COMMUNICATION,
      title: "...", detail: "...", proposed_action: "...", task: package,
      on_reject: [ { wait: 0.5, do: log, agent: atlas, text: "Package held for revision." } ] }
  - { wait: 1.0, do: mission_report, executive_summary: "...", objective_status: PARTIAL,
      key_findings: [...], conflicts: [...], assumptions: [...], needs_human_attention: [...],
      next_actions: [...] }
  - { wait: 0.5, do: phase, phase: CLOSED }
```

## Step types

| `do` | fields | notes |
|---|---|---|
| `phase` | `phase` | a `MissionPhase` |
| `task` | `ref, title, description, assigned_to, priority?, depends_on?[refs], requires_approval?, approval_reason?` | `ref` is a scenario-local name; the runner maps it to a real task id |
| `task_update` | `ref, status?, progress?` | sets `started_at` / `completed_at` automatically |
| `agent` | `agent, status, activity?, task?[ref], with?[agent ids]` | `with` → `collaborating_with` |
| `message` | `from, to, type, subject, body, task?, confidence?, requires_response?` | |
| `report` | AgentReport fields, `task` as ref | |
| `approval` | `ref, requested_by, reason, title, detail, proposed_action?, task?, on_reject?[steps]` | **the runner pauses** until `POST /approvals/{id}/decision`; if rejected it plays `on_reject` then continues |
| `mission_report` | MissionReport fields | agent report ids and task lists are filled in automatically |
| `log` | `agent?, text` | activity feed line |

`wait` is seconds before the step runs, divided by the mission's `speed` (default 1).

## Rules

- Every agent referenced must exist in the registry **and** be allowed in the scenario's node.
- `depends_on`, `task` and `ref` must reference tasks created earlier in the same scenario.
- A scenario ends with `phase: CLOSED`. The runner sets every agent it touched back to `IDLE`
  (`MONITORING` for ARGOS).
- ORACLE's findings must tag each claim as FACT / ASSUMPTION / SCENARIO / RECOMMENDATION.
