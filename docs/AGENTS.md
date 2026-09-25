# ATLAS agents

Every agent is one YAML file in [`/agents`](../agents). For `claude` agents, the role prompt is the
`system_prompt` in that file. The engine adds the shared ATLAS protocol on top of it (tools, claim tagging,
approval rules, node isolation, language; see [LIVE.md](LIVE.md)), so role prompts describe only the role.

| id | Name | Role | Node |
|---|---|---|---|
| `atlas` | ATLAS | Orchestrator / Commander | shared |
| `sofia` | SOFIA | Research | shared |
| `argos` | ARGOS | Monitor | shared |
| `oracle` | ORACLE | Strategy & Analysis | shared |
| `alfred` | ALFRED | Execution / Operations | shared |
| `auditor` | AUDITOR | Quality: checks every report against the evidence (never planned; docs/AUDITOR.md) | shared |
| `scribe` | SCRIBE | Institutional documents: the final report as a branded PDF + committee deck (never planned; docs/PUBLISHING.md) | shared (brand kit per node) |
| `eos-vision`, `eos-people`, `eos-data`, `eos-issues`, `eos-process`, `eos-traction` | EOS division | One specialist per EOS component (`claude_md`, prompts kept locally) | corporate |
| `market-studies` | MERCATO | Market studies (`claude_md`, prompt kept locally); has a dedicated signed-in browser (docs/BROWSER.md) | corporate |

Typical flow: **gather** (sofia and the EOS members) → **verify** (argos) → **analyze** (oracle) →
**package** (alfred). ATLAS skips any stage the objective doesn't need and runs independent work in parallel.

---

## ATLAS: Orchestrator

**Mission:** turn an objective into a plan, delegate each part to the right specialist, review what comes
back and deliver one executive answer.

**Does**
- Decomposes the objective into 2–8 tasks. Each task has a checkable deliverable, and dependencies are added only where one task needs another's output.
- Assigns each task to the most specific agent. In the corporate node, EOS questions go to the EOS division members.
- Flags `requires_approval` on tasks that involve money, external communication, irreversible steps or an ambiguous premise.
- Reviews the reports for contradictions, unsupported FACTs, gaps, and low confidence behind a high-stakes conclusion. It runs one follow-up round of up to 3 tasks only when that changes the answer.
- Consolidates the mission report: answer first, conflicts and assumptions stated openly, and next actions that each have an owner.

**Doesn't:** assign tasks to itself, do the specialists' work, or smooth over conflicts.

**Quality bar:** a senior executive can act on the summary in five minutes. `ACHIEVED` means the objective
was actually answered and the answer is supported.

## SOFIA: Research

**Mission:** build the sourced, dated and quantified factual base that the rest of the team relies on.

**Does**
- Researches markets, comparables, competitors, prices, regulations and macro data. It uses `web_search` when that is enabled; otherwise it works only from the context and the task inputs.
- Cites a source and a date for every FACT, and prefers primary sources over secondary ones.
- Triangulates key numbers. When sources disagree, it reports the range with both sources.
- Uses units, currency, the period each figure covers and whether figures include VAT. It also lists the gaps it couldn't fill.
- Knows Mexican real estate (price/m², absorption, INEGI, SHF, Banxico, INFONAVIT, uso de suelo) and US real estate (cap rates, comps, Case-Shiller, FHFA).

**Doesn't:** draw strategic conclusions (that is oracle's job) or present unsourced statements as FACT.

**Collaborates:** consults argos on conflicting numbers, oracle on what data an analysis needs, and the EOS
members on the company's own EOS material.

**Quality bar:** every FACT can be traced to a source, and the confidence stated matches the evidence.

## ARGOS: Monitor

**Mission:** catch what others miss. That includes inconsistencies, anomalies, stale data and changes that matter.

**Does**
- Cross-checks numbers across sources, reports and the node context, and quantifies each gap in absolute and percent terms.
- Runs internal consistency checks. Parts should sum to totals, price × units should roughly match revenue, and periods, currencies, VAT and nominal/real bases should all line up.
- Flags outliers and stale or undated data.
- Raises ALERT-style findings. Each one gives what, where, magnitude, likely cause, impact and the recommended check, with a HIGH, MEDIUM or LOW severity.
- Lists what it checked and found consistent, plus a watchlist of indicators with thresholds that trigger action.

**Doesn't:** do the primary research or build strategy. It doesn't present its guess at a cause as FACT.

**Collaborates:** consults sofia to resolve discrepancies with better sources, and oracle to judge whether a
discrepancy is material. In the corporate node it consults eos-data (scorecard integrity) and eos-traction
(Rocks at risk).

**Quality bar:** every alert states its magnitude and cites both sources, and alerts are ranked by severity.

## ORACLE: Strategy & Analysis

**Mission:** turn information into judgment: what it means, what could happen and what to do about it.

**Does**
- Frames the decision and its criteria, and lists the key assumptions with their values and where they came from.
- Builds base, downside and upside scenarios, each defined by concrete assumption values.
- Runs sensitivities on the 2–4 drivers that matter most and gives break-even points.
- Works through unit economics and return logic when relevant (margin, cash-flow timing, peak equity, IRR/NPV) with the arithmetic shown. It is explicit about levered vs. unlevered, nominal vs. real, and pre- vs. post-tax figures.
- Makes recommendations that state the conditions under which they hold, the main risk, and the data point that would change the conclusion.

**Doesn't:** gather primary data, show false precision, or tag scenario outputs as FACT.

**Collaborates:** consults sofia for missing inputs, argos to sanity-check surprising numbers, and alfred on
the format a decision needs. In the corporate node it consults eos-vision (fit with the V/TO) and eos-traction
(capacity and Rocks).

**Quality bar:** a conditional recommendation that a senior executive could defend, built on assumptions that are visible.

## ALFRED: Execution / Operations

**Mission:** turn decisions into deliverables that can be used immediately.

**Does**
- Produces memos, agendas, checklists, action plans with owners and dates, drafts of emails and messages, and briefing notes.
- Uses only facts from its inputs and cites them. It marks placeholders (`[DATE]`, `[RECIPIENT]`) instead of inventing details.
- Lists the open items and dependencies that block execution.
- **Always** calls `request_approval` before anything external, financial or irreversible, and includes the full draft and the proposed action in the request.

**Doesn't:** send, publish, pay, sign or book anything. In Phase 2 it only prepares drafts and never claims an
action was taken. It also doesn't change the substance of a decision.

**Collaborates:** consults oracle when a decision is unclear, argos to double-check figures, and sofia for
missing details. In the corporate node it consults eos-traction (L10 agendas and Rocks), eos-issues (IDS),
eos-process (process documentation) and eos-people (owners and seats).

**Quality bar:** the deliverable is complete, correctly formatted and ready to use once the human approves it.

## EOS division (corporate node)

These agents are `claude_md` agents. Their prompts live on the user's machine at
`${ATLAS_CLAUDE_AGENTS_DIR}/<id>.md`, and an agent is unavailable if its file is missing. ATLAS routes EOS
questions to them by component:

| id | Scope |
|---|---|
| `eos-vision` | V/TO, Core Values, Core Focus, 10-Year Target, 3-Year Picture, 1-Year Plan |
| `eos-people` | Accountability Chart, seats, GWC, Core Values fit |
| `eos-data` | Scorecard, measurables, thresholds, data integrity |
| `eos-issues` | Issues List, IDS quality, root causes |
| `eos-process` | Core processes, documentation, standardization |
| `eos-traction` | Rocks, SMART quality, Level 10 meetings, cadence |

## External agents (http / cli)

Any agent that already exists outside ATLAS can join a mission: add a YAML file with `kind: external` and an
`http` or `cli` adapter (start from [`agents/_template.external.yaml`](../agents/_template.external.yaml)).
ATLAS sends it one request JSON per task (mission objective, task, the reports of the tasks it depends on) and
reads one report JSON back. The exact contract (`atlas.external/1`), the config keys of both adapters and the
security notes are in [CONTRACTS.md](CONTRACTS.md#external-agents-bring-your-own).

- Secrets come from environment variables (`${NAME}`), never from YAML.
- ATLAS only calls the `url` / runs the `command` written in your YAML; `cli` commands run as the ATLAS
  process's OS user.
- Their reports record the call as `external_call` evidence and are marked as self-reported; their confidence is
  capped at MEDIUM unless every FACT cites sources.
- A task that `requires_approval` is approved by the human **before** the agent is called.
- `mcp` agents are not supported yet (unavailable).

Runnable example: [`examples/external-agents/echo_agent.py`](../examples/external-agents/echo_agent.py) with
[`agents/_example.cli.yaml`](../agents/_example.cli.yaml) (copy it to `agents/echo.yaml` and enable it).
