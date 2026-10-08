"""Sales brochures: a project's own visual identity, 16:9 pages, printed by Chromium (docs/BROCHURES.md).

IRIS (the sales-material agent) writes WHAT goes on each page — a spec of page types filled with copy, figures
and images; this module decides HOW it looks: fixed, tested layouts in the project's brand kit, rendered as HTML
and printed to PDF at 1440×810 px by Chromium. Nothing is placed by hand, so nothing is "corrido".

Project brand kits are private (they hold the project's identity): <ATLAS_LOCAL_DIR>/brand/<slug>/brand.yaml

    name: Torre Acqua
    tagline: La forma más pura de vivir.
    colors: {paper: "#FFFFF8", deep: "#043447", primary: "#2B5C63", accent: "#69ACB7", tint: "#E8F1F2"}
    fonts:                         # files in the kit (ttf/otf/woff2); missing ones fall back to serif / sans
      display: {light: fonts/CormorantGaramond-Light.ttf, italic: fonts/CormorantGaramond-LightItalic.ttf}
      body: {light: fonts/Jost-Light.ttf, regular: fonts/Jost-Regular.ttf, medium: fonts/Jost-Medium.ttf}
    logo_dark: logo_navy.png       # on light pages
    logo_light: logo_white.png     # on dark pages and photos
    isotype: isotipo.png           # the mark used as a large watermark (optional)
    shape: drop                    # how feature images are cut: drop | arch | rect
    footer: Torre Acqua · Monterrey

Lessons built in (from the Torre Acqua brochure):
- no transparency anywhere (no rgba, opacity, blend modes): some PDF viewers composite it wrong (pink covers).
  Text over a photo gets its shadow baked into the JPEG; watermarks are pre-blended with each page's color.
- images are shown whole where they are evidence (plans, tables) and filled where they are mood (renders).
- every text box shrinks to fit; whatever still overflows is reported back by page.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core import paths

log = logging.getLogger("atlas.publish")

W, H = 1440, 810
MAX_PAGES = 40
SHAPES = ("drop", "arch", "rect")
IMAGE_MAX_PX = 2400
_HEX = re.compile(r"^#?[0-9A-Fa-f]{6}$")

DEFAULT_COLORS = {
    "paper": "#FBFAF6",    # light pages
    "deep": "#16263A",     # dark pages, headings on light
    "primary": "#2F4F5F",  # titles, lines
    "accent": "#7FA7B5",   # eyebrows, details
    "tint": "#EDF1F2",     # soft pages, cards
    "ink": "#2B3640",      # body text on light
    "muted": "#7C8790",    # captions, footers
    "rule": "#C9D3D6",     # hairlines on light
}

# ----------------------------------------------------------------------------------------------------------------
# brand kit
# ----------------------------------------------------------------------------------------------------------------


@dataclass
class BrochureKit:
    slug: str
    folder: Path | None
    name: str = ""
    tagline: str = ""
    footer: str = ""
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    fonts: dict[str, dict[str, Path]] = field(default_factory=dict)  # display/body -> weight -> file
    logo_dark: Path | None = None
    logo_light: Path | None = None
    isotype: Path | None = None
    shape: str = "arch"
    brief: Path | None = None  # the project's approved facts and copy (read it first)
    assets: Path | None = None  # the project's approved, finished images and maps
    configured: bool = False

    def c(self, role: str) -> str:
        return self.colors.get(role) or DEFAULT_COLORS[role]


def kits_dir() -> Path:
    return paths.local_dir() / "brand"


def kit_slugs() -> list[str]:
    """Project kits on this machine (folders with a brand.yaml that declares fonts or a shape)."""
    root = kits_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        y = d / "brand.yaml"
        if y.is_file():
            try:
                data = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict) and ("shape" in data or "fonts" in data and isinstance(data["fonts"], dict)
                                           and "display" in data["fonts"]):
                out.append(d.name)
    return out


def _color(v: Any, default: str) -> str:
    s = str(v or "").strip()
    return ("#" + s.lstrip("#")).upper() if _HEX.match(s) else default


def load_kit(slug: str) -> BrochureKit:
    slug = re.sub(r"[^a-z0-9_-]", "", str(slug or "").lower())
    folder = kits_dir() / slug if slug else None
    kit = BrochureKit(slug=slug or "neutral", folder=folder if folder and folder.is_dir() else None)
    if kit.folder is None:
        return kit
    try:
        data = yaml.safe_load((kit.folder / "brand.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("brochure kit %s unreadable: %s", slug, exc)
        return kit

    def file(rel: Any) -> Path | None:
        if not rel:
            return None
        p = (kit.folder / str(rel)).resolve()  # type: ignore[operator]
        try:
            p.relative_to(kit.folder.resolve())  # type: ignore[union-attr]
        except ValueError:
            return None
        return p if p.is_file() else None

    kit.configured = True
    kit.name = str(data.get("name") or slug)
    kit.tagline = str(data.get("tagline") or "")
    kit.footer = str(data.get("footer") or kit.name)
    for role, value in (data.get("colors") or {}).items():
        if role in DEFAULT_COLORS:
            kit.colors[role] = _color(value, DEFAULT_COLORS[role])
    for family in ("display", "body"):
        weights = (data.get("fonts") or {}).get(family) or {}
        kit.fonts[family] = {w: f for w, f in ((w, file(v)) for w, v in weights.items()) if f}
    kit.logo_dark = file(data.get("logo_dark"))
    kit.logo_light = file(data.get("logo_light")) or kit.logo_dark
    kit.logo_dark = kit.logo_dark or kit.logo_light
    kit.isotype = file(data.get("isotype"))
    kit.shape = data.get("shape") if data.get("shape") in SHAPES else "arch"
    kit.brief = file(data.get("brief") or "brief.md")
    assets = (kit.folder / str(data.get("assets") or "assets")).resolve()
    kit.assets = assets if assets.is_dir() and str(assets).startswith(str(kit.folder.resolve())) else None
    return kit


# ----------------------------------------------------------------------------------------------------------------
# spec
# ----------------------------------------------------------------------------------------------------------------

PAGE_TYPES = ("cover", "hero", "statement", "number", "list", "split", "columns", "plan", "table", "plans",
              "logos", "gallery", "image", "closing", "contact")
TONES = ("light", "tint", "dark")

_T = {"type": "string"}
_IMG = {"type": "string", "description": "path of an image you can read (onedrive:/…, imagenes/…, extracted/…)"}
_ITEM = {"type": "object", "properties": {"title": _T, "text": _T}, "required": ["title"]}
_STAT = {"type": "object", "properties": {"value": _T, "label": _T}, "required": ["value", "label"]}

BROCHURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "kit (the project's brand kit slug) + pages. Titles: 'first line|second line' — the second "
                   "line is set in italics (the brochure's voice: 'La forma|más pura de vivir.').",
    "properties": {
        "kit": {"type": "string", "description": "brand kit slug, e.g. 'acqua' (brand/<slug>/ on this machine)"},
        "title": _T,
        "pages": {
            "type": "array", "maxItems": MAX_PAGES,
            "items": {
                "type": "object",
                "description": (
                    "cover {title, subtitle?, image? (photo cover) } · hero {image, eyebrow?, title} (full-bleed "
                    "photo, title bottom-left) · statement {eyebrow?, title, text, image? (cut in the kit's shape)} · "
                    "number {number, title, text?, stats[≤4]} · list {eyebrow?, title, text?, items[{title, text}] "
                    "≤8, two columns} · split {eyebrow?, title, text, image, side: left|right, shape?: "
                    "drop|arch|rect, stats?} · columns {eyebrow?, title, columns[{heading, items[]}] ≤3} · "
                    "plan {eyebrow?, title, items[{title,text}] ≤8, image (a floor plan, shown whole)} · table "
                    "{eyebrow?, title, table{columns, rows}, note?} · plans {eyebrow?, title, plans[{image, "
                    "label, caption?}] ≤3, shown whole} · logos {eyebrow?, title, text?, stats?, logos[{image, "
                    "label?}] ≤36} · gallery {eyebrow?, title, text?, images[2] (arches)} · image {image, "
                    "caption?} (a full-page image: map, aerial) · closing {title, text?, image?} · contact "
                    "{title?, lines[], legal?}. tone: light | tint | dark on any page."),
                "properties": {
                    "type": {"type": "string", "enum": list(PAGE_TYPES)},
                    "tone": {"type": "string", "enum": list(TONES)},
                    "eyebrow": _T, "title": _T, "subtitle": _T, "text": _T, "number": _T, "caption": _T,
                    "note": _T, "legal": _T, "image": _IMG,
                    "side": {"type": "string", "enum": ["left", "right"]},
                    "shape": {"type": "string", "enum": list(SHAPES)},
                    "stats": {"type": "array", "items": _STAT},
                    "items": {"type": "array", "items": _ITEM},
                    "columns": {"type": "array", "items": {"type": "object", "properties": {
                        "heading": _T, "items": {"type": "array", "items": _T}}, "required": ["heading", "items"]}},
                    "table": {"type": "object", "properties": {
                        "columns": {"type": "array", "items": _T},
                        "rows": {"type": "array", "items": {"type": "array", "items": {"type": ["string", "number"]}}}},
                        "required": ["columns", "rows"]},
                    "plans": {"type": "array", "items": {"type": "object", "properties": {
                        "image": _IMG, "label": _T, "caption": _T}, "required": ["image"]}},
                    "logos": {"type": "array", "items": {"type": "object", "properties": {
                        "image": _IMG, "label": _T}, "required": ["image"]}},
                    "images": {"type": "array", "items": _IMG},
                    "lines": {"type": "array", "items": _T},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["kit", "pages"],
}


def _s(v: Any, n: int = 1200) -> str:
    return " ".join(str(v if v is not None else "").split())[:n]


def _texts(v: Any, n: int, width: int = 200) -> list[str]:
    return [_s(x, width) for x in (v if isinstance(v, list) else []) if _s(x, width)][:n]


def normalize(data: Any) -> tuple[dict[str, Any], list[str]]:
    """(clean spec, errors). Pages missing what their type needs are dropped and reported."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return {"kit": "", "title": "", "pages": []}, ["the brochure must be an object {kit, pages}"]
    pages: list[dict[str, Any]] = []
    raw_pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    if len(raw_pages) > MAX_PAGES:
        errors.append(f"at most {MAX_PAGES} pages ({len(raw_pages)} given)")
    for i, p in enumerate(raw_pages[:MAX_PAGES], 1):
        if not isinstance(p, dict):
            continue
        t = str(p.get("type") or "").lower()
        if t not in PAGE_TYPES:
            errors.append(f"page {i}: unknown type '{t}'")
            continue
        out: dict[str, Any] = {
            "type": t, "tone": p.get("tone") if p.get("tone") in TONES else "",
            "eyebrow": _s(p.get("eyebrow"), 60), "title": _s(p.get("title"), 160), "subtitle": _s(p.get("subtitle"), 200),
            "text": _s(p.get("text"), 900), "number": _s(p.get("number"), 12), "caption": _s(p.get("caption"), 200),
            "note": _s(p.get("note"), 300), "legal": _s(p.get("legal"), 1500), "image": _s(p.get("image"), 1000),
            "side": "left" if p.get("side") == "left" else "right",
            "shape": p.get("shape") if p.get("shape") in SHAPES else "",
            "stats": [{"value": _s(x.get("value"), 16), "label": _s(x.get("label"), 60)}
                      for x in (p.get("stats") or []) if isinstance(x, dict) and _s(x.get("value"))][:4],
            "items": [{"title": _s(x.get("title"), 80), "text": _s(x.get("text"), 220)}
                      for x in (p.get("items") or []) if isinstance(x, dict) and _s(x.get("title"))][:8],
            "columns": [{"heading": _s(c.get("heading"), 50), "items": _texts(c.get("items"), 10, 80)}
                        for c in (p.get("columns") or []) if isinstance(c, dict) and _s(c.get("heading"))][:3],
            "plans": [{"image": _s(x.get("image"), 1000), "label": _s(x.get("label"), 40),
                       "caption": _s(x.get("caption"), 90)}
                      for x in (p.get("plans") or []) if isinstance(x, dict) and _s(x.get("image"))][:3],
            "logos": [{"image": _s(x.get("image"), 1000), "label": _s(x.get("label"), 40)}
                      for x in (p.get("logos") or []) if isinstance(x, dict) and _s(x.get("image"))][:36],
            "images": _texts(p.get("images"), 2, 1000),
            "lines": _texts(p.get("lines"), 8, 120),
        }
        tb = p.get("table") if isinstance(p.get("table"), dict) else {}
        cols = _texts(tb.get("columns"), 6, 40)
        rows = [[_s(c, 60) for c in r[: len(cols)]] for r in (tb.get("rows") or []) if isinstance(r, list)][:10]
        out["table"] = {"columns": cols, "rows": rows} if cols and rows else None
        need = {
            "cover": out["title"], "hero": out["image"] and out["title"], "statement": out["title"],
            "number": out["number"] and out["title"], "list": out["title"] and out["items"],
            "split": out["title"] and out["image"], "columns": out["title"] and out["columns"],
            "plan": out["title"] and out["image"], "table": out["title"] and out["table"],
            "plans": out["title"] and out["plans"], "logos": out["title"] and out["logos"],
            "gallery": out["title"] and len(out["images"]) == 2, "image": out["image"],
            "closing": out["title"], "contact": out["lines"] or out["title"],
        }[t]
        if not need:
            errors.append(f"page {i} ({t}) is missing what it needs — see the page types")
            continue
        pages.append(out)
    if not pages:
        errors.append("the brochure has no valid pages")
    return {"kit": _s(data.get("kit"), 40), "title": _s(data.get("title"), 120), "pages": pages}, errors


def image_refs(spec: dict[str, Any]) -> list[str]:
    """Every image the spec uses (resolved through the agent's file sandbox before rendering)."""
    refs: list[str] = []
    for p in spec["pages"]:
        refs += [p["image"]] if p["image"] else []
        refs += p["images"] + [x["image"] for x in p["plans"]] + [x["image"] for x in p["logos"]]
    return list(dict.fromkeys(r for r in refs if r))


# ----------------------------------------------------------------------------------------------------------------
# images: opaque, RGB, sized — and shadows / watermarks baked in (no CSS transparency)
# ----------------------------------------------------------------------------------------------------------------


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(x * (1 - t) + y * t):02X}" for x, y in zip(ra, rb, strict=True))


def prepare_photo(src: Path, out_dir: Path, *, shade: str | None = None, flatten_on: str | None = None) -> Path:
    """An RGB JPEG ≤ IMAGE_MAX_PX (CMYK fixed). `shade`: bake a bottom-left gradient toward that color into the
    pixels (legible white text over a photo without CSS transparency). `flatten_on`: images with alpha (logos,
    plans) are composited onto that solid color and saved opaque."""
    from PIL import Image, ImageOps

    key = hashlib.sha1(f"{src}|{src.stat().st_mtime}|{shade}|{flatten_on}".encode()).hexdigest()[:16]
    with Image.open(src) as im0:
        im = ImageOps.exif_transpose(im0)
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if im.mode == "CMYK":
            im = im.convert("RGB")
        if has_alpha:
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (*_rgb(flatten_on or "#FFFFFF"), 255))
            bg.alpha_composite(im)
            im = bg.convert("RGB")
        else:
            im = im.convert("RGB")
        im.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX))
        if shade:
            im = _bake_shade(im, shade)
        out = out_dir / (f"{key}.png" if has_alpha else f"{key}.jpg")
        if has_alpha:
            im.save(out, "PNG", optimize=True)
        else:
            im.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return out


def prepare_logo(src: Path, out_dir: Path, bg: str, ink: str) -> Path:
    """A logo flattened onto the page color. A logo that would vanish there (a white logo on a light page, a navy
    one on a dark page) is recolored to `ink` first, keeping its shape (alpha)."""
    from PIL import Image

    key = hashlib.sha1(f"logo|{src}|{src.stat().st_mtime}|{bg}|{ink}".encode()).hexdigest()[:16]
    out = out_dir / f"{key}.png"
    if out.exists():
        return out
    with Image.open(src) as im0:
        im = im0.convert("RGBA")
    im.thumbnail((900, 900))
    alpha = im.getchannel("A")
    lum = im.convert("L")
    hist = lum.histogram(mask=alpha.point(lambda a: 255 if a > 128 else 0))
    n = sum(hist)

    def luminance(hex_: str) -> float:
        r, g, b = _rgb(hex_)
        return (0.299 * r + 0.587 * g + 0.114 * b)

    if n and abs(sum(i * c for i, c in enumerate(hist)) / n - luminance(bg)) < 90:
        im = Image.merge("RGBA", (*Image.new("RGB", im.size, _rgb(ink)).split(), alpha))
    base = Image.new("RGBA", im.size, (*_rgb(bg), 255))
    base.alpha_composite(im)
    base.convert("RGB").save(out, "PNG", optimize=True)
    return out


def _bake_shade(im: Any, color: str) -> Any:
    from PIL import Image

    w, h = im.size
    grad = Image.linear_gradient("L").resize((w, h))  # 0 at the top → 255 at the bottom
    ramp = grad.point(lambda v: int(max(0, (v - 120)) * 1.35) if v > 120 else 0)
    side = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h))  # 255 on the left → 0 on the right
    side = side.point(lambda v: int(v * 0.45))
    from PIL import ImageChops

    mask = ImageChops.lighter(ramp, side).point(lambda v: min(200, v))
    return Image.composite(Image.new("RGB", (w, h), _rgb(color)), im, mask)


def watermark(iso: Path, out_dir: Path, bg: str, fg: str) -> Path:
    """The isotype as a large outline mark pre-blended onto the page color (opaque PNG): fg mixed 9% into bg."""
    from PIL import Image

    key = hashlib.sha1(f"{iso}|{bg}|{fg}".encode()).hexdigest()[:16]
    out = out_dir / f"wm-{key}.png"
    if out.exists():
        return out
    with Image.open(iso) as im0:
        im = im0.convert("RGBA")
    alpha = im.getchannel("A")
    if alpha.getextrema() == (255, 255):  # no transparency: use darkness as the mark
        alpha = im.convert("L").point(lambda v: 255 - v)
    tint = Image.new("RGB", im.size, _rgb(_mix(bg, fg, 0.09)))
    base = Image.new("RGB", im.size, _rgb(bg))
    Image.composite(tint, base, alpha).save(out, "PNG", optimize=True)
    return out


# ----------------------------------------------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------------------------------------------

_DROP = "M0.5,0 C0.5,0 0.06,0.47 0.06,0.69 C0.06,0.86 0.26,1 0.5,1 C0.74,1 0.94,0.86 0.94,0.69 C0.94,0.47 0.5,0 0.5,0 Z"


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def _title(t: str, cls: str = "h2") -> str:
    """'first|second' → first line roman, second in italics."""
    if "|" in t:
        a, b = t.split("|", 1)
        return f'<h2 class="{cls}">{_e(a.strip())}<br><em>{_e(b.strip())}</em></h2>'
    return f'<h2 class="{cls}">{_e(t)}</h2>'


def _font_faces(kit: BrochureKit) -> str:
    out = []
    weights = {"light": (300, "normal"), "regular": (400, "normal"), "medium": (500, "normal"),
               "italic": (300, "italic"), "lightitalic": (300, "italic"), "regularitalic": (400, "italic")}
    for family, files in kit.fonts.items():
        for w, path in files.items():
            weight, style = weights.get(w, (400, "normal"))
            out.append(f"@font-face{{font-family:'K-{family}';src:url('{path.as_uri()}');font-weight:{weight};"
                       f"font-style:{style};}}")
    return "\n".join(out)


def _css(kit: BrochureKit) -> str:
    c = kit.c
    return _font_faces(kit) + f"""
@page {{ size: {W}px {H}px; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ background: {c('paper')}; }}
body {{ font-family: 'K-body', 'Jost', 'Helvetica Neue', Arial, sans-serif; font-weight: 300; color: {c('ink')};
       -webkit-font-smoothing: antialiased; }}
.page {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; break-after: page;
        background: {c('paper')}; }}
.page.tint {{ background: {c('tint')}; }}
.page.dark {{ background: {c('deep')}; color: {_mix(c('deep'), '#FFFFFF', 0.82)}; }}
.eyebrow {{ font-size: 11px; letter-spacing: .34em; text-transform: uppercase; color: {c('accent')};
           font-weight: 500; margin-bottom: 22px; }}
.h1 {{ font-family: 'K-display', 'Cormorant Garamond', Georgia, serif; font-weight: 300; font-size: 76px;
      line-height: 1.02; color: {c('deep')}; }}
.h2 {{ font-family: 'K-display', 'Cormorant Garamond', Georgia, serif; font-weight: 300; font-size: 56px;
      line-height: 1.04; color: {c('deep')}; }}
.h2 em, .h1 em {{ font-style: italic; color: {c('primary')}; }}
.dark .h1, .dark .h2, .photo .h1, .photo .h2 {{ color: #FFFFFF; }}
.dark .h1 em, .dark .h2 em, .photo .h2 em, .photo .h1 em {{ color: {_mix(c('accent'), '#FFFFFF', 0.35)}; }}
.rule {{ width: 48px; height: 1px; background: {c('accent')}; margin: 26px 0; }}
.body {{ font-size: 14.5px; line-height: 1.75; max-width: 470px; }}
.dark .body {{ color: {_mix(c('deep'), '#FFFFFF', 0.78)}; }}
.foot {{ position: absolute; left: 72px; right: 72px; bottom: 30px; display: flex; justify-content: space-between;
        font-size: 9.5px; letter-spacing: .28em; text-transform: uppercase; color: {c('muted')}; }}
.dark .foot, .photo .foot {{ color: {_mix(c('deep'), '#FFFFFF', 0.55)}; }}
.wm {{ position: absolute; right: -60px; top: -40px; height: 900px; }}
.text {{ position: absolute; left: 96px; top: 120px; width: 560px; bottom: 90px; overflow: hidden; }}
.text.right {{ left: auto; right: 96px; }}
.media {{ position: absolute; top: 0; bottom: 0; width: 640px; overflow: hidden; }}
.media.right {{ right: 0; }} .media.left {{ left: 0; }}
.media img, .fill {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.shape {{ position: absolute; top: 80px; bottom: 80px; width: 470px; }}
.shape.right {{ right: 120px; }} .shape.left {{ left: 120px; }}
.shape img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.shape.arch img {{ border-radius: 999px 999px 0 0; }}
.shape.drop img {{ clip-path: url(#dropclip); }}
.stats {{ display: flex; gap: 46px; margin-top: 34px; }}
.stat .v {{ font-family: 'K-display', Georgia, serif; font-size: 44px; font-weight: 300; color: {c('deep')};
           line-height: 1; }}
.dark .stat .v {{ color: #FFFFFF; }}
.stat .l {{ font-size: 9.5px; letter-spacing: .26em; text-transform: uppercase; color: {c('muted')};
           margin-top: 10px; max-width: 150px; line-height: 1.5; }}
.items {{ display: grid; grid-template-columns: 1fr 1fr; column-gap: 48px; margin-top: 8px; }}
.items.one {{ grid-template-columns: 1fr; }}
.item {{ border-top: 1px solid {c('rule')}; padding: 13px 0 14px; }}
.dark .item {{ border-top-color: {_mix(c('deep'), '#FFFFFF', 0.25)}; }}
.item .t {{ font-family: 'K-display', Georgia, serif; font-size: 22px; color: {c('deep')}; font-weight: 300; }}
.dark .item .t {{ color: #FFFFFF; }}
.item .d {{ font-size: 12.5px; line-height: 1.55; color: {c('muted')}; margin-top: 3px; }}
.cols {{ display: grid; grid-template-columns: repeat(3, 1fr); column-gap: 56px; margin-top: 42px; }}
.col h4 {{ font-size: 11px; letter-spacing: .3em; text-transform: uppercase; color: {c('accent')};
          font-weight: 500; padding-bottom: 14px; border-bottom: 1px solid {c('accent')}; }}
.col li {{ list-style: none; font-size: 14px; padding: 10px 0; border-bottom: 1px solid {c('rule')}; }}
.dark .col li {{ border-bottom-color: {_mix(c('deep'), '#FFFFFF', 0.2)}; }}
.card {{ position: absolute; background: #FFFFFF; border: 1px solid {c('rule')}; }}
.card img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
table.typ {{ width: 100%; border-collapse: collapse; margin-top: 34px; font-size: 14px; }}
table.typ th {{ text-align: left; font-size: 10px; letter-spacing: .26em; text-transform: uppercase;
               color: {c('accent')}; font-weight: 500; padding: 0 12px 14px 0;
               border-bottom: 1px solid {c('deep')}; }}
table.typ td {{ padding: 15px 12px 15px 0; border-bottom: 1px solid {c('rule')}; }}
table.typ td:first-child {{ font-family: 'K-display', Georgia, serif; font-size: 21px; color: {c('deep')}; }}
table.typ td.n, table.typ th.n {{ text-align: right; }}
.note {{ font-size: 11px; color: {c('muted')}; margin-top: 16px; line-height: 1.6; }}
.plans {{ position: absolute; left: 72px; right: 72px; top: 250px; bottom: 80px; display: flex; gap: 32px; }}
.plans .pl {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
.plans .pl .box {{ flex: 1; min-height: 0; overflow: hidden; background: #FFFFFF; border: 1px solid {c('rule')}; }}
.plans .pl .box img {{ width: 100%; height: 100%; object-fit: contain; display: block; padding: 14px; }}
.plans .lab {{ font-family: 'K-display', Georgia, serif; font-size: 22px; color: {c('deep')}; margin-top: 14px; }}
.plans .cap {{ font-size: 11.5px; color: {c('muted')}; margin-top: 3px; }}
.logos {{ position: absolute; right: 72px; top: 110px; width: 760px; bottom: 90px; display: grid;
         grid-template-columns: repeat(6, 1fr); grid-auto-rows: 74px; gap: 14px 18px; align-content: center; }}
.logos .lg {{ display: flex; flex-direction: column; align-items: center; justify-content: center; }}
.logos .lg img {{ max-width: 100%; max-height: 46px; object-fit: contain; }}
.logos.few {{ grid-template-columns: repeat(var(--n), 1fr); grid-auto-rows: 220px; gap: 40px; }}
.logos.few .lg img {{ max-height: 130px; }}
.logos .lg span {{ font-size: 9px; letter-spacing: .2em; color: {c('muted')}; margin-top: 6px; }}
.gallery {{ position: absolute; right: 100px; top: 120px; bottom: 90px; display: flex; gap: 26px; }}
.gallery img {{ width: 300px; height: 100%; object-fit: cover; border-radius: 999px 999px 0 0; }}
.cap-full {{ position: absolute; left: 72px; bottom: 64px; font-size: 11px; letter-spacing: .3em;
            text-transform: uppercase; color: #FFFFFF; }}
.center {{ position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
          justify-content: center; text-align: center; }}
.center .body {{ max-width: 620px; }}
"""


class _Pages:
    def __init__(self, kit: BrochureKit, img: dict[str, Path], tmp: Path):
        self.kit, self.img, self.tmp = kit, img, tmp
        self.n = 0

    def src(self, ref: str, **kw: Any) -> str:
        p = self.img.get(ref)
        if p is None:
            return ""
        if kw:
            p = prepare_photo(p, self.tmp, **kw)
        return p.as_uri()

    def logo_src(self, ref: str, bg: str, ink: str) -> str:
        p = self.img.get(ref)
        return prepare_logo(p, self.tmp, bg, ink).as_uri() if p is not None else ""

    def bg_of(self, tone: str) -> str:
        return {"dark": self.kit.c("deep"), "tint": self.kit.c("tint")}.get(tone, self.kit.c("paper"))

    def wm(self, tone: str) -> str:
        if not self.kit.isotype:
            return ""
        bg = self.bg_of(tone)
        fg = "#FFFFFF" if tone == "dark" else self.kit.c("deep")
        return f'<img class="wm" src="{watermark(self.kit.isotype, self.tmp, bg, fg).as_uri()}">'

    def foot(self, light_text: bool = False) -> str:
        return (f'<div class="foot"><span>{_e(self.kit.footer)}</span><span class="pn">{self.n:02d}</span></div>')

    def logo(self, dark_bg: bool, height: int = 96, style: str = "") -> str:
        p = self.kit.logo_light if dark_bg else self.kit.logo_dark
        if not p:
            return ""
        bg = self.kit.c("deep") if dark_bg else self.kit.c("paper")
        src = prepare_photo(p, self.tmp, flatten_on=bg).as_uri()
        return f'<img src="{src}" style="height:{height}px;{style}">'

    def page(self, p: dict[str, Any]) -> str:
        self.n += 1
        default_tone = {"cover": "light", "closing": "dark", "number": "light"}.get(p["type"], "light")
        tone = p["tone"] or default_tone
        body = getattr(self, "p_" + p["type"])(p, tone)
        cls = "page " + ({"dark": "dark", "tint": "tint"}.get(tone, "")) + (" photo" if p["type"] in
                                                                             ("hero", "image") else "")
        return f'<section class="{cls}" data-n="{self.n}">{body}</section>'

    # -- page types -------------------------------------------------------------------------------------------

    def _eyebrow(self, p: dict[str, Any]) -> str:
        return f'<div class="eyebrow">{_e(p["eyebrow"])}</div>' if p["eyebrow"] else ""

    def _stats(self, stats: list[dict[str, str]]) -> str:
        if not stats:
            return ""
        return '<div class="stats">' + "".join(
            f'<div class="stat"><div class="v">{_e(s["value"])}</div><div class="l">{_e(s["label"])}</div></div>'
            for s in stats) + "</div>"

    def _items(self, items: list[dict[str, str]], one: bool = False) -> str:
        return f'<div class="items{" one" if one else ""}">' + "".join(
            f'<div class="item"><div class="t">{_e(i["title"])}</div>'
            + (f'<div class="d">{_e(i["text"])}</div>' if i["text"] else "") + "</div>" for i in items) + "</div>"

    def p_cover(self, p: dict[str, Any], tone: str) -> str:
        if p["image"]:
            return (f'<img class="fill" src="{self.src(p["image"], shade=self.kit.c("deep"))}">'
                    f'<div style="position:absolute;left:96px;bottom:110px;width:820px">'
                    f'{self.logo(True, 110)}<div class="rule"></div>{_title(p["title"], "h1")}'
                    + (f'<p class="body" style="color:#FFFFFF;margin-top:20px">{_e(p["subtitle"])}</p>'
                       if p["subtitle"] else "") + "</div>")
        dark = tone == "dark"
        return (self.wm(tone) + '<div class="center">' + self.logo(dark, 150)
                + f'<div style="margin-top:34px">{_title(p["title"], "h2")}</div>'
                + (f'<div class="eyebrow" style="margin-top:22px">{_e(p["subtitle"])}</div>' if p["subtitle"] else "")
                + "</div>" + self.foot())

    def p_hero(self, p: dict[str, Any], tone: str) -> str:
        return (f'<img class="fill" src="{self.src(p["image"], shade=self.kit.c("deep"))}">'
                f'<div style="position:absolute;left:96px;bottom:96px;width:760px">{self._eyebrow(p)}'
                f'{_title(p["title"], "h1")}</div>' + self.foot())

    def _feature(self, p: dict[str, Any], side: str) -> str:
        shape = p["shape"] or self.kit.shape
        src = self.src(p["image"])
        if shape == "rect":
            return f'<div class="media {side}"><img src="{src}"></div>'
        return f'<div class="shape {shape} {side}"><img src="{src}"></div>'

    def p_statement(self, p: dict[str, Any], tone: str) -> str:
        if not p["image"]:
            return (self.wm(tone) + '<div class="center">' + self._eyebrow(p) + _title(p["title"])
                    + '<div class="rule"></div>' + (f'<p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                    + "</div>" + self.foot())
        return (self._feature(p, "right")
                + f'<div class="text" style="top:200px">{self._eyebrow(p)}{_title(p["title"])}'
                + '<div class="rule"></div>' + (f'<p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                + "</div>" + self.foot())

    def p_number(self, p: dict[str, Any], tone: str) -> str:
        num_color = "#FFFFFF" if tone == "dark" else self.kit.c("primary")
        return (self.wm(tone)
                + f'<div style="position:absolute;left:96px;top:150px;font-family:\'K-display\',Georgia,serif;'
                  f'font-weight:300;font-size:260px;line-height:.9;color:{num_color}">{_e(p["number"])}</div>'
                + f'<div class="text" style="left:640px;top:200px;width:620px">{self._eyebrow(p)}{_title(p["title"])}'
                + (f'<div class="rule"></div><p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                + self._stats(p["stats"]) + "</div>" + self.foot())

    def p_list(self, p: dict[str, Any], tone: str) -> str:
        return (self.wm(tone) + f'<div class="text" style="width:440px">{self._eyebrow(p)}{_title(p["title"])}'
                + (f'<div class="rule"></div><p class="body">{_e(p["text"])}</p>' if p["text"] else "") + "</div>"
                + f'<div class="text" style="left:620px;width:720px;top:150px">{self._items(p["items"])}</div>'
                + self.foot())

    def p_split(self, p: dict[str, Any], tone: str) -> str:
        side = p["side"]
        text_side = "right" if side == "left" else ""
        style = "top:170px" + (";right:96px" if text_side else "")
        return (self._feature(p, side)
                + f'<div class="text {text_side}" style="{style}">{self._eyebrow(p)}{_title(p["title"])}'
                + '<div class="rule"></div>' + (f'<p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                + self._stats(p["stats"]) + "</div>" + self.foot())

    def p_columns(self, p: dict[str, Any], tone: str) -> str:
        cols = "".join(f'<div class="col"><h4>{_e(c["heading"])}</h4><ul>'
                       + "".join(f"<li>{_e(i)}</li>" for i in c["items"]) + "</ul></div>" for c in p["columns"])
        return (f'<div style="position:absolute;left:96px;right:96px;top:100px;bottom:90px" class="fitbox">'
                f'{self._eyebrow(p)}{_title(p["title"])}<div class="cols">{cols}</div></div>' + self.foot())

    def p_plan(self, p: dict[str, Any], tone: str) -> str:
        return (f'<div class="text" style="width:420px;top:110px">{self._eyebrow(p)}{_title(p["title"])}'
                f'<div class="rule"></div>{self._items(p["items"], one=True)}</div>'
                f'<div class="card" style="left:560px;right:72px;top:90px;bottom:90px;padding:18px">'
                f'<img src="{self.src(p["image"], flatten_on="#FFFFFF")}"></div>' + self.foot())

    def p_table(self, p: dict[str, Any], tone: str) -> str:
        tb = p["table"]
        numeric = [all(re.match(r"^[\d,.\s$%m²+—–-]*$", str(r[i] if i < len(r) else "")) for r in tb["rows"])
                   and i > 0 for i in range(len(tb["columns"]))]
        head = "".join(f'<th class="{"n" if numeric[i] else ""}">{_e(c)}</th>' for i, c in enumerate(tb["columns"]))
        rows = "".join("<tr>" + "".join(f'<td class="{"n" if numeric[i] else ""}">{_e(str(v))}</td>'
                                        for i, v in enumerate(r)) + "</tr>" for r in tb["rows"])
        return (f'<div class="fitbox" style="position:absolute;left:96px;right:96px;top:100px;bottom:90px">{self._eyebrow(p)}'
                f'{_title(p["title"])}<div><table class="typ"><thead><tr>{head}</tr></thead>'
                f'<tbody>{rows}</tbody></table>' + (f'<p class="note">{_e(p["note"])}</p>' if p["note"] else "")
                + "</div></div>" + self.foot())

    def p_plans(self, p: dict[str, Any], tone: str) -> str:
        pls = "".join(
            f'<div class="pl"><div class="box"><img src="{self.src(x["image"], flatten_on="#FFFFFF")}"></div>'
            + (f'<div class="lab">{_e(x["label"])}</div>' if x["label"] else "")
            + (f'<div class="cap">{_e(x["caption"])}</div>' if x["caption"] else "") + "</div>" for x in p["plans"])
        return (f'<div style="position:absolute;left:72px;top:96px;width:900px">{self._eyebrow(p)}'
                f'{_title(p["title"])}</div><div class="plans">{pls}</div>' + self.foot())

    def p_logos(self, p: dict[str, Any], tone: str) -> str:
        bg = self.bg_of(tone)
        ink = "#FFFFFF" if tone == "dark" else self.kit.c("deep")
        logos = "".join(f'<div class="lg"><img src="{self.logo_src(x["image"], bg, ink)}">'
                        + (f"<span>{_e(x['label'])}</span>" if x["label"] else "") + "</div>" for x in p["logos"])
        return (f'<div class="text" style="width:500px">{self._eyebrow(p)}{_title(p["title"])}'
                + (f'<div class="rule"></div><p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                + self._stats(p["stats"]) + (f'</div><div class="logos few" style="--n:{len(p["logos"])}">'
                                              if len(p["logos"]) <= 4 else '</div><div class="logos">')
                + f"{logos}</div>" + self.foot())

    def p_gallery(self, p: dict[str, Any], tone: str) -> str:
        imgs = "".join(f'<img src="{self.src(i)}">' for i in p["images"])
        return (f'<div class="text" style="top:200px;width:480px">{self._eyebrow(p)}{_title(p["title"])}'
                + (f'<div class="rule"></div><p class="body">{_e(p["text"])}</p>' if p["text"] else "")
                + f'</div><div class="gallery">{imgs}</div>' + self.foot())

    def p_image(self, p: dict[str, Any], tone: str) -> str:
        shade = self.kit.c("deep") if p["caption"] else None
        return (f'<img class="fill" src="{self.src(p["image"], shade=shade) if shade else self.src(p["image"])}">'
                + (f'<div class="cap-full">{_e(p["caption"])}</div>' if p["caption"] else ""))

    def p_closing(self, p: dict[str, Any], tone: str) -> str:
        img = ""
        if p["image"]:
            shape = p["shape"] or self.kit.shape
            img = (f'<div class="shape {shape}" style="left:50%;margin-left:-190px;width:380px;top:70px;'
                   f'bottom:250px"><img src="{self.src(p["image"])}"></div>')
        top = "560px" if p["image"] else "300px"
        return (self.wm(tone) + img + f'<div style="position:absolute;left:0;right:0;top:{top};text-align:center">'
                + _title(p["title"]) + (f'<p class="body" style="margin:18px auto 0">{_e(p["text"])}</p>'
                                        if p["text"] else "") + "</div>" + self.foot())

    def p_contact(self, p: dict[str, Any], tone: str) -> str:
        lines = "".join(f'<div style="font-size:15px;line-height:2">{_e(x)}</div>' for x in p["lines"])
        return (f'<div style="position:absolute;left:96px;top:0;bottom:0;width:420px;display:flex;'
                f'align-items:center">{self.logo(tone == "dark", 170)}</div>'
                f'<div class="text" style="left:640px;top:180px;width:640px">'
                + (f'<div class="eyebrow">{_e(p["title"])}</div>' if p["title"] else "") + lines
                + (f'<p class="note" style="margin-top:40px">{_e(p["legal"])}</p>' if p["legal"] else "")
                + "</div>" + self.foot())


_FIT_JS = """
() => {
  const issues = [];
  document.querySelectorAll('section.page').forEach((pg) => {
    const n = pg.dataset.n;
    // shrink every fitting box until its content fits (down to 70%)
    pg.querySelectorAll('.text, .fitbox').forEach((el) => {
      let scale = 1;
      const base = new Map();
      el.querySelectorAll('*').forEach((c) => base.set(c, parseFloat(getComputedStyle(c).fontSize)));
      base.set(el, parseFloat(getComputedStyle(el).fontSize));
      const over = () => el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4;
      while (over() && scale > 0.7) {
        scale -= 0.04;
        base.forEach((size, c) => { c.style.fontSize = (size * scale) + 'px'; });
      }
      if (over()) issues.push(`page ${n}: text doesn't fit even at 70% (${(el.textContent || '').trim().slice(0, 60)}…)`);
    });
    const r = pg.getBoundingClientRect();
    pg.querySelectorAll('h2, p, .item, li, td, .stat').forEach((el) => {
      const b = el.getBoundingClientRect();
      if (b.width && (b.right > r.right + 1 || b.bottom > r.bottom - 20 || b.left < r.left - 1)) {
        issues.push(`page ${n}: "${(el.textContent || '').trim().slice(0, 50)}" runs off the page`);
      }
    });
  });
  return [...new Set(issues)];
}
"""


@dataclass
class Rendered:
    pdf: bytes
    previews: list[bytes]  # one PNG per page (small), for the agent to look at
    warnings: list[str]


def build_html(spec: dict[str, Any], kit: BrochureKit, images: dict[str, Path], tmp: Path) -> str:
    pages = _Pages(kit, images, tmp)
    body = "\n".join(pages.page(p) for p in spec["pages"])
    clip = (f'<svg width="0" height="0" style="position:absolute"><defs><clipPath id="dropclip" '
            f'clipPathUnits="objectBoundingBox"><path d="{_DROP}"/></clipPath></defs></svg>')
    title = _e(spec.get("title") or kit.name)
    return (f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>{title}</title>"
            f"<style>{_css(kit)}</style></head><body>{clip}{body}</body></html>")


def render(spec: dict[str, Any], kit: BrochureKit, images: dict[str, Path], *, preview_width: int = 480) -> Rendered:
    """Print the brochure with Chromium. `images` maps each spec image ref to a local file."""
    from playwright.sync_api import sync_playwright

    from ..live import browser as B
    from ..live.siteimages import _launch

    B._proactor_on_windows()
    with tempfile.TemporaryDirectory(prefix="atlas-brochure-") as td:
        tmp = Path(td)
        missing = [r for r in image_refs(spec) if r not in images]
        doc = tmp / "brochure.html"
        doc.write_text(build_html(spec, kit, images, tmp), encoding="utf-8")
        with sync_playwright() as pw:
            br = _launch(pw)
            try:
                page = br.new_page(viewport={"width": W, "height": H})
                page.goto(doc.as_uri(), wait_until="load")
                page.evaluate("document.fonts.ready")
                warnings = list(page.evaluate(_FIT_JS))
                pdf = page.pdf(width=f"{W}px", height=f"{H}px", print_background=True,
                               margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                previews = []
                for el in page.query_selector_all("section.page"):
                    previews.append(el.screenshot(type="png"))
            finally:
                br.close()
    warnings += [f"image not found: {r}" for r in missing]
    if not kit.configured:
        warnings.append(f"no brand kit '{kit.slug}' on this machine (brand/{kit.slug}/brand.yaml): neutral style used")
    return Rendered(pdf, [_shrink(p, preview_width) for p in previews], warnings)


def _shrink(png: bytes, width: int) -> bytes:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGB")
        im.thumbnail((width, width))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def contact_sheet(previews: list[bytes], cols: int = 4, width: int = 1568) -> bytes:
    """All pages in one image (numbered), so the agent can look at the whole brochure in one read."""
    import io

    from PIL import Image, ImageDraw

    if not previews:
        return b""
    ims = [Image.open(io.BytesIO(p)).convert("RGB") for p in previews]
    cw = width // cols
    ch = round(cw * H / W)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cw * cols, (ch + 18) * rows), "#FFFFFF")
    d = ImageDraw.Draw(sheet)
    for i, im in enumerate(ims):
        im = im.resize((cw - 6, ch - 6))
        x, y = (i % cols) * cw, (i // cols) * (ch + 18)
        sheet.paste(im, (x + 3, y + 3))
        d.text((x + 6, y + ch + 2), f"{i + 1}", fill="#333333")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()
