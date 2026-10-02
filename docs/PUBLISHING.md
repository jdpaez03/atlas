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

## Briefs

The recurring briefs go through SCRIBE as well:

- **Monday L10 brief (ARGOS):** a PDF and a deck to project in the L10 meeting. The deck has the week in one
  sentence, the key figures, one slide per area (Rocks, project dashboards, Level 10, follow-ups) and the Rocks
  status table. The `.docx` is still produced (editable). Download it from Monitor → L10 brief, or at
  `GET /briefs/{id}/documents/{name}`.
- **CC digest (HERMES):** a PDF with one section per conversation: what happened, what you are asked (a
  callout), decisions and figures. Download it from Follow-ups → Digest, or at `GET /digests/{id}/documents/{name}`.

These briefs are already checked content: ARGOS's figures are computed and its prose passes a number guard,
and the digest keeps only figures found verbatim in the emails. So SCRIBE does not rewrite them. The document
is built from them directly (no model call) and only laid out with the brand kit. `ATLAS_PUBLISH=off` turns
these documents off too, and `ATLAS_PUBLISH_FORMATS` applies (the digest is PDF only).

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

## Style settings (per document)

SCRIBE sets a `style` on each document, usually because of an approved lesson (docs/LESSONS.md):

| Setting | Effect | Default |
| --- | --- | --- |
| `deck_density` | `airy` / `standard` / `compact`: text size on slides | `standard` |
| `table_rows_per_slide` | long deck tables continue on the next slide instead of being cut (4-14) | 8 |
| `agenda` | an agenda slide listing the sections, after the cover | off |
| `toc` | a table of contents with page numbers in the PDF | off |
| `chart_data_labels` | values printed on bars and points (PDF and deck) | off |
| `section_numbers` | 01, 02… before PDF sections | on |

SCRIBE's PAGA prompt lives privately in `<ATLAS_LOCAL_DIR>/agents/scribe.md`. It replaces the public prompt in
`agents/scribe.yaml`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_PUBLISH` | `on` | `off` = never publish |
| `ATLAS_PUBLISH_FORMATS` | `pdf,pptx` | either one or both |
| `ATLAS_PUBLISH_MAX_TOKENS` | `16000` | output budget for SCRIBE's spec |

Per mission: the **Institutional documents** checkbox when you launch a live mission (API: `"publish": false`).

## Decks and PDFs made by agents during a mission

SCRIBE formats the final report when a mission closes. Any agent can also produce a branded document during the mission, for example ALFRED building a 15-slide investor deck from an approved script.

- **How:** call `write_deliverable` with `format: "pptx"` or `"pdf"` and a `document`. The `document` is the same spec SCRIBE submits:
  - `pptx` renders `slides`, and adds the cover and closing slides.
  - `pdf` renders `summary` + `sections`.
- **Look:** the node's brand kit is used, so the result looks like SCRIBE's documents.
- **Where it goes:** the file lands in the mission outputs, like any other deliverable.
- **Inputs from earlier missions:** a mission can read the node's earlier deliverables in place (`outputs/<node>/<mission_id>/`, read-only). An approved script or workbook from a previous mission needs no copying.
- **Photos and renders:** `cover_image`, image slides and image blocks.
  - `cover_image` puts a photo on the cover (right third).
  - Image slides: `{type: image, image, title, items?, caption, layout: side|full}`. `side` puts the photo on the right and the text on the left; `full` is a full-bleed photo with a title band.
  - Image blocks in the PDF: `{type: image, image, caption}`.
  - `image` is the path of a png, jpg, webp, gif, bmp or tif that the agent can read (local roots, `onedrive:` / `sharepoint:`, earlier missions' outputs). It goes through the same sandbox as `read_file`, and images of other nodes or outside the roots are refused.
  - A reference that can't be used becomes an "Imagen no disponible" placeholder, and the agent is told why.
  - Images that aren't the company's own are credited in the caption ("Fuente: <domain>"); the prompt says so. SCRIBE's end-of-mission documents carry no images, because SCRIBE reads no files.
  - **Images from websites:** `collect_site_images(url)` opens any public web page in a headless browser. It scrolls to load lazy galleries and saves every image at least `min_px` (default 600 px) on its longest side into the mission outputs.
    - Sources: `<img>` with the largest `srcset` candidate, `<picture>`, CSS backgrounds and `og:image`.
    - Icons, logos, SVGs and duplicates are skipped.
    - It also returns the page's links to the same site, so the agent can walk from the home page to each project page.
    - Where images land depends on the site:
      - The company's own sites (`ATLAS_OWN_SITES`, e.g. `pagadesarrollos.com`) save to `imagenes/<name>`, as company material.
      - Any other site (a project microsite, a partner) saves to `imagenes/<domain>/<name>`. Those images are used too, with "Fuente: <domain>" in the caption.
    - `imagenes/fuentes.csv` records each image's origin, URL and page, so every picture can be credited.
    - Private addresses are never opened: localhost, the LAN and the tailnet are all refused.
    - No login, a fresh browser each call (never the agents' signed-in profiles).
- **Charts:** `chart_type` `bar` or `line` take `categories` + `series` of values. `scatter` takes `points`, each `{label, x, y, group?}`, plus `x_title` and `y_title`. Use it for positioning charts, e.g. $/m² vs ticket, the project vs its comparables.
  - Each point is labeled.
  - `group` colors the points (e.g. "PAGA" vs "Competencia"). At most 4 groups and 40 points.
  - Axes use round bounds.
  - In the deck it is a native, editable PowerPoint XY chart. Groups are padded to the same length because LibreOffice otherwise hides a shorter first group.
