# Lessons: agents that improve from your comments

Every agent (ATLAS, SOFIA, ARGOS, ORACLE, ALFRED, HERMES, MERCATO, the EOS division, AUDITOR and SCRIBE)
follows the **lessons** you approve. A lesson is a short rule that is appended to that agent's instructions in
every run: missions, follow-up rounds, the L10 brief, the CC digest, drafts, audits and SCRIBE's documents.

## The Lessons tray (tab 5)

1. Pick an agent, or **All agents**, and write what you'd like to change, in your own words:
   *"In the committee deck the tables are too long and the text is small."*
2. **Propose rule:** ATLAS turns the comment into one to three general, actionable rules. It uses the fast
   model and sees the agent's current lessons. If a new rule refines or contradicts an existing lesson, it
   marks that lesson as *replaced*.
3. The proposals wait under **Waiting for your approval**. You can:
   - **Approve** it: it becomes active from the agent's next run, and any lesson it replaces is retired.
   - **Edit** it, then approve.
   - **Discard** it.

   Nothing changes an agent's behaviour without your OK.
4. **Save as rule:** your text is already the rule, so it becomes active right away.
5. **Active lessons** are listed by agent. Hover to **edit** or **retire** one. Discarded and retired lessons
   stay in the history and can be restored.

Without a live backend (no Claude plan or API key), a comment is proposed as written, for you to edit and
approve.

## What a lesson can and cannot do

- **It refines how the agent works and presents results:** structure, detail, tone, scenarios to always
  include, terminology.
- **It never overrides the rules on evidence and honesty.** Sources, labelling estimates as estimates and
  flagging untraced figures stay in force. The prompt says so explicitly, and the distiller refuses to weaken
  them.
- **SCRIBE's layout lessons map to style settings** on each document (docs/PUBLISHING.md):
  - `deck_density`
  - `table_rows_per_slide`
  - `agenda`
  - `toc`
  - `chart_data_labels`
  - `section_numbers`

  So "letra más grande" or "índice en el PDF" really changes the file, not just the wording.

## Storage

Lessons live in `<ATLAS_LOCAL_DIR>/lessons.json`. The file is private, hand-editable and survives a history
reset. The store mirrors it for the UI (`lesson.upserted` events).

API:
- `GET /lessons?agent_id=&status=`
- `POST /lessons {agent_id, text, mode: comment|direct, node?}`
- `PATCH /lessons/{id} {status?, text?}`

A lesson can be limited to one node (`node`); otherwise it applies everywhere.

## Private prompts

Any built-in agent's public prompt (in `agents/*.yaml`) can be replaced by a private one at
`<ATLAS_LOCAL_DIR>/agents/<agent-id>.md`, where frontmatter is optional. Use it for company-specific
instructions that should not live in the public repo; SCRIBE's PAGA prompt is kept there, for example. Lessons
are added on top of whichever prompt is in use.
