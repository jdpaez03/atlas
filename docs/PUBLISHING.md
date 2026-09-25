# Institutional documents (SCRIBE)

When a live mission closes, **SCRIBE** turns the final report into the documents you hand to a committee, a
bank or an investor:

- a **PDF report**: a cover, the executive summary with the headline figures, numbered sections (text, tables,
  charts, KPI tiles, callouts), and an end page with notes, sources and verification;
- a **committee deck** (`.pptx`, editable): a cover, the answer in one sentence, the key figures, section
  slides, tables, native charts, bullets and quotes, then a closing slide, with speaker notes.

Both files use the node's visual identity. They land in the mission outputs, appear under **Institutional
documents** in the report, and are recorded as evidence.

## How it works

1. After consolidation (and after AUDITOR), SCRIBE receives the final report, every agent report, excerpts of
   the data files the agents delivered (csv / xlsx / md …) and your notes in the thread.
2. It writes one structured spec (`submit_documents`): the title, the summary, the highlights, the sections
   with their blocks, and the slides. It decides content and structure only, never layout. It is told to use
   only figures that appear in those inputs, to keep estimates labelled as estimates, and to cite a source on
   every table and chart.
3. The system renders the spec with the brand kit (`atlas/publish/`: `pdf.py` with reportlab, `deck.py` with
   python-pptx).
4. **Traceability check:** every figure in the documents is compared against the agent reports, the evidence,
   the data files and your notes. Any figure that traces to none of them is logged. It is also listed on the
   PDF's verification page ("Cifra sin respaldo rastreable…"). Nothing is silently dropped.
5. If SCRIBE fails (for example a usage limit or an invalid spec), the mission report still goes out, without
   documents, and the reason appears in the log.

A follow-up round publishes new versions (`…_v2_Reporte.pdf`).

## Brand kit (private, per node)

```
<ATLAS_LOCAL_DIR>/brand/<node>/
  brand.yaml        company, side label, footer, colors, deck and PDF fonts
  logo_light.png    logo for dark backgrounds (cover, closing)
  logo_dark.png     logo for white pages (running header)
  deck_base.pptx    the company's PowerPoint template, without its slides
  fonts/            optional .ttf files for the PDF
```

A node without a kit gets a neutral style, so the public repo holds no company's identity. To build
`deck_base.pptx` from a company template (`.potx`, `.potm` or `.pptx`; any macros are dropped):

```
uv run python -c "from pathlib import Path; from atlas.publish.brand import deck_base_from_template as d; \
d(Path('PAGA TEMPLATE.potm'), Path('../../../atlas-local/brand/corporate/deck_base.pptx'))"
```

**Fonts.** Decks use `deck.font` / `deck.font_strong` by name, so the font must be installed on the PCs that
open the deck. The default, Segoe UI Light / Semibold, ships with every Windows. If the brand fonts (for
PAGA: Produkt / Graphik) are installed everywhere, set them in `brand.yaml`. The PDF embeds its fonts:
`pdf_fonts: auto` uses Segoe UI on Windows; point a role at a `.ttf` in `fonts/` to use the brand's.

The document labels ("Resumen ejecutivo", "Notas y fuentes", "Fuente") are in Spanish.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_PUBLISH` | `on` | `off` = never publish |
| `ATLAS_PUBLISH_FORMATS` | `pdf,pptx` | either one or both |
| `ATLAS_PUBLISH_MAX_TOKENS` | `16000` | output budget for SCRIBE's spec |

Per mission: the **Institutional documents** checkbox when you launch a live mission (API: `"publish": false`).
