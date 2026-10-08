# IRIS: sales brochures

IRIS (`agents/corporate/iris.yaml`) makes the sales brochure of a project: a 16:9 PDF in the project's own identity, for a buyer's first contact.

It is not for technical or committee material. Market studies are MERCATO's job; institutional documents are SCRIBE's.

Code:
- `apps/api/atlas/publish/brochure.py`: page types, the project kit, and the Chromium render.
- `apps/api/atlas/live/designtools.py`: IRIS's tools.

The standard is the Torre Acqua brochure (29 pages, v11).

## How it works

1. **IRIS fills the content; the system lays it out.**
   - IRIS writes a spec: one entry per page, with a type and its content (copy, figures, images).
   - The renderer builds HTML with fixed layouts in the project's kit and prints it with Chromium at 1440×810 px.
   - Nothing is placed by hand.
2. **Text that doesn't fit shrinks.** Each text box can shrink down to 70%. Whatever still overflows comes back as a warning with its page number.
3. **IRIS checks its own output.** `write_brochure` returns a contact sheet of every page. IRIS looks at it and writes `_v2`, `_v3`… until it's clean.
4. **No transparency anywhere.** Some PDF viewers composite transparency wrong (the pink Acqua cover). So there's no `rgba`, `opacity` or blend modes:
   - the shadow behind text on a photo is baked into the JPEG;
   - the watermark is pre-blended with each page's color;
   - logos are flattened onto the page's background.

## Page types

`tone: light | tint | dark` works on every page. A title written as `first line|second line` sets the second line in italics.

| Type | For | Fields |
|---|---|---|
| `cover` | cover (watermark, or a photo) | `title`, `subtitle`, `image?` |
| `hero` | full-bleed render with the slogan | `image`, `eyebrow`, `title` |
| `statement` | idea + image cut in the kit's shape (drop / arch) | `eyebrow`, `title`, `text`, `image?` |
| `number` | big number (50 levels, +15 hectares) + stats | `number`, `title`, `text`, `stats[≤4]` |
| `list` | location or features in two columns | `title`, `text`, `items[{title,text}] ≤8` |
| `split` | text + image (side, shape) | `title`, `text`, `image`, `side`, `shape`, `stats?` |
| `columns` | amenities in up to 3 columns | `title`, `columns[{heading, items}]` |
| `plan` | a level plan, shown whole | `title`, `items`, `image` |
| `table` | typologies (m² interior / terrace / total) | `title`, `table{columns, rows}`, `note` |
| `plans` | 3 floor plans per page, shown whole | `title`, `plans[{image,label,caption}]` |
| `logos` | developer track record | `title`, `text`, `stats`, `logos[{image,label}] ≤36` |
| `gallery` | two images in arches | `title`, `text`, `images[2]` |
| `image` | full-page image (map, aerial) | `image`, `caption?` |
| `closing` | closing phrase (+ image in the shape) | `title`, `text`, `image?` |
| `contact` | logo, contact, legal note | `lines[]`, `legal` |

## Project brand kit

Kits are private and live on each machine: `<ATLAS_LOCAL_DIR>/brand/<slug>/`, for example `brand/acqua/`.

```yaml
name: Torre Acqua
footer: Torre Acqua
colors: {paper: "#FFFFF8", deep: "#043447", primary: "#2B5C63", accent: "#69ACB7", tint: "#E7F0F1"}
fonts:
  display: {light: fonts/display-light.woff2, italic: fonts/display-italic.woff2}   # Cormorant Garamond
  body: {light: fonts/body-300.woff2, regular: fonts/body-400.woff2, medium: fonts/body-500.woff2}  # Jost
logo_dark: logo_navy.png      # on light pages
logo_light: logo_white.png    # on dark pages and photos
isotype: isotipo.png          # watermark
shape: drop                   # drop | arch | rect
```

Color roles:

| Role | Used for |
|---|---|
| `paper` | light pages |
| `deep` | dark pages and titles |
| `primary` | the italic line |
| `accent` | eyebrows and lines |
| `tint` | soft pages |
| `ink` | text |
| `muted` | captions |
| `rule` | hairlines |

Without a kit, the brochure comes out in a neutral style with a warning. IRIS asks for the brandbook before writing.

The kit can also hold what was already worked out for the project. IRIS reads both before touching any source:

| Item | What it holds |
|---|---|
| `brief.md` | The approved facts (confirmed figures, so IRIS doesn't ask again), the criteria, and the approved copy and page sequence. |
| `assets/` | The finished images and maps: skies already corrected, concept map, unit plans. |

The `brand/` folder can be read by the agents. Developer folders sit alongside the project kits, for the track record:
- `brand/paga/`: `logo_white.png`, `trayectoria.md` (32 developments with country and year) and `logos/<year>_<project>.png`.
- `brand/dags/`: `logo.png` and `trayectoria.md` (11 projects with city).

Logos stay visible on any page: a white logo on a light page (or a navy one on a dark page) is recolored to the page's ink, keeping its shape. With four logos or fewer, the `logos` page shows them large (for example the PAGA + DAGS alliance).

## IRIS's tools

Only agents with the `sales_brochure` capability get them.

| Tool | What it does |
|---|---|
| `pdf_images(path, pages?, min_px?)` | Extracts the images embedded in a PDF into `extraidas/<pdf>/`, with their real colors. PDFium decodes them, so CMYK images don't come out inverted. Returns a numbered contact sheet. Source PDFs can be up to 150 MB. |
| `page_image(path, page, dpi?)` | One page as an image in `paginas/`. The view comes back with a 10% grid for reading crop boxes; the saved file has no grid. |
| `crop_image(path, box, name, transparent?)` | Cuts a region (fractions 0–1) into `recortes/`. `transparent` turns the flat background into alpha, for logos. |
| `write_brochure(filename, brochure)` | The PDF in the mission outputs, plus the contact sheet. It refuses working notes and tool names on the pages. |

IRIS has a 45-turn budget (`max_turns` in its YAML).

## Criteria (in IRIS's prompt)

- **No investment terms.** No discounts, ROI, IRR or appreciation unless the mission asks for them.
- **Source of truth:** the architect's schematic for units and areas. When two sources disagree, IRIS asks; it doesn't pick one.
- **Images:**
  - a moodboard is never presented as a render of the project;
  - no photo is repeated;
  - if images are missing, IRIS says so.
- **Copy:** unconfirmed facts are written cautiously ("uno de los pocos"). Fields the human must fill are left as visible placeholders (`[Nombre del asesor]`).

## Out of scope for now

Done by hand or asked of the human:
- recoloring renders (pink skies);
- the concept map generated from Google Maps.
