# Corporate memory and Ask ATLAS

ATLAS keeps up with what has been done in a node and with what you tell it, so a new mission builds on earlier
work instead of starting from zero, and you can simply ask it what happened.

## What it remembers

| Source | What | Where |
| --- | --- | --- |
| **Earlier work** | every closed mission's latest report (summary, key findings, next actions, open points, documents, topics), the L10 briefs and the CC digests | derived from the history (nothing extra stored) |
| **Knowledge notes** | facts you state or approve: "Josué lleva Balcones desde septiembre", "el comité aprobó Pietra" | `<ATLAS_LOCAL_DIR>/knowledge.json` (dated, per node) |
| **Lessons** | how each agent should work (docs/LESSONS.md) | `<ATLAS_LOCAL_DIR>/lessons.json` |
| **Node context** | fixed background you maintain | `<ATLAS_LOCAL_DIR>/context/<node>/` |

At consolidation, ATLAS tags each mission with its **topics**: projects, areas and people (for example
"Balcones 800", "crédito puente"). Topics weigh most in the search.

ATLAS's own runs (inbox scans, ARGOS watches, the brief and digest runs) are not indexed as missions. Their
results are indexed as the brief or digest they produce.

## In missions

When a mission starts, and when you open a follow-up round, ATLAS searches the node's history for related
work. The search matches words without accents and gives more weight to titles and topics, with a recency
boost. The best matches and all your active knowledge notes go to the orchestrator (planning, review,
consolidation) and to every agent's task, under **Prior knowledge**. The mission log shows `ATLAS recalls …`.

Prior knowledge is **context, not evidence**:
- agents build on it and say whether this run confirms, updates or contradicts it;
- any figure they rely on must be re-verified in this run, or cited as "Misión <id> (<date>)" and labelled
  ASSUMPTION;
- AUDITOR flags an earlier figure presented as a current FACT as *stale*.

## Ask ATLAS (tab 0)

A conversation with ATLAS about the node. It answers from the related earlier work, your knowledge notes, the
live state and the conversation so far. The live state is:
- missions in progress;
- open and overdue follow-ups;
- open ARGOS alerts;
- Rocks off track;
- the latest L10 brief.

Each answer can include:
- **Sources:** chips for the missions, briefs or digests it relied on. Click one to open it.
- **Remember?** When you *tell* it something durable ("el comité ya pidió negociar retorno preferente"), it
  proposes saving it as a knowledge note. Only facts you stated, and only if you click **Save**.
- **Needs new work:** when the answer isn't in memory, it says so and proposes a mission objective. **Launch
  mission** starts it.

It never invents. What it doesn't know, it says. The conversation is kept per node in
`<ATLAS_LOCAL_DIR>/ask/<node>.json`, and **New conversation** archives it and starts fresh.

The right-hand panels list **What ATLAS knows from you** (add a note, or *forget* one) and **Memory**, a
searchable index of everything it can recall.

## API

- `POST /ask {node, question}` returns the new turns; `GET /ask?node=`; `POST /ask/clear?node=`
- `GET|POST /knowledge`, `PATCH /knowledge/{id} {status, text}`
- `GET /memory?node=&q=` returns the index, newest first, or the best matches for `q`

Ask ATLAS uses the default model. Its calls are not counted in a mission's usage.
