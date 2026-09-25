"""The brand kit a node's institutional documents are rendered with (docs/PUBLISHING.md).

Private, per machine and per node: <ATLAS_LOCAL_DIR>/brand/<node>/

    brand.yaml        company, colors, fonts, footer (every key optional)
    logo_light.png    logo for dark backgrounds (cover, closing slide)
    logo_dark.png     logo for light pages (running header)
    deck_base.pptx    the company's PowerPoint template with its slides removed (theme, size, fonts)
    fonts/*.ttf       optional: the brand's font files for the PDF

A node with no kit gets a neutral style, so the repo holds no company's identity.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core import paths

_HEX = re.compile(r"^#?[0-9A-Fa-f]{6}$")

NEUTRAL_COLORS = {
    "primary": "#1F2A44",   # large fields, headings, table headers
    "dark": "#111827",      # section slides
    "ink": "#374151",       # body text
    "muted": "#8A9099",     # captions, labels, sources
    "light": "#F4F5F7",     # zebra rows, callouts, light slides
    "rule": "#D6D8DC",      # hairlines
    "accent": "#9CA8B8",    # secondary series, details
}

# PDF fonts: first existing file wins. Segoe UI ships with every Windows (Light / Semilight / Semibold);
# DejaVu is the Linux fallback; the PDF core fonts are the last resort.
_SELAWIK = "/usr/local/share/fonts/selawik/"  # Segoe UI metric-compatible (OFL), installed by install-server.sh
_FONT_CANDIDATES = {
    "light": ["C:/Windows/Fonts/segoeuil.ttf", _SELAWIK + "selawkl.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf"],
    "regular": ["C:/Windows/Fonts/segoeuisl.ttf", "C:/Windows/Fonts/segoeui.ttf", _SELAWIK + "selawksl.ttf",
                _SELAWIK + "selawk.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "semibold": ["C:/Windows/Fonts/seguisb.ttf", _SELAWIK + "selawksb.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "italic": ["C:/Windows/Fonts/seguili.ttf", "C:/Windows/Fonts/segoeuii.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"],
}
_CORE_FONTS = {"light": "Helvetica", "regular": "Helvetica", "semibold": "Helvetica-Bold",
               "italic": "Helvetica-Oblique"}


def _color(value: Any, default: str) -> str:
    v = str(value or "").strip()
    return ("#" + v.lstrip("#")).upper() if _HEX.match(v) else default


@dataclass
class Brand:
    node: str
    folder: Path | None
    company: str = ""
    side_label: str = ""
    footer: str = "Confidencial"
    prepared_by: str = "Elaborado con ATLAS"
    colors: dict[str, str] = field(default_factory=lambda: dict(NEUTRAL_COLORS))
    deck_font: str = "Segoe UI Light"
    deck_font_strong: str = "Segoe UI Semibold"
    pdf_fonts: dict[str, str] = field(default_factory=dict)  # role -> ttf path or core font name
    logo_light: Path | None = None
    logo_dark: Path | None = None
    deck_base: Path | None = None
    configured: bool = False

    def c(self, role: str) -> str:
        return self.colors.get(role) or NEUTRAL_COLORS[role]

    @property
    def label(self) -> str:
        return self.side_label or self.company.upper()


def brand_dir(node: str) -> Path:
    return paths.local_dir() / "brand" / paths.safe_name(node)


def _file(folder: Path, value: Any, default: str) -> Path | None:
    name = str(value or default).strip()
    p = Path(name) if os.path.isabs(name) else folder / name
    return p if p.is_file() else None


def resolve_pdf_fonts(folder: Path | None, configured: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for role, candidates in _FONT_CANDIDATES.items():
        wanted = str(configured.get(role) or "auto").strip()
        chosen = ""
        if wanted and wanted != "auto":
            p = Path(wanted) if os.path.isabs(wanted) or folder is None else folder / wanted
            if p.is_file():
                chosen = str(p)
        if not chosen:
            windir = os.environ.get("WINDIR", "C:/Windows").replace("\\", "/")
            for cand in candidates:
                cand = cand.replace("C:/Windows", windir) if sys.platform == "win32" else cand
                if Path(cand).is_file():
                    chosen = cand
                    break
        out[role] = chosen or _CORE_FONTS[role]
    return out


def load_brand(node: str) -> Brand:
    folder = brand_dir(node)
    cfg: dict[str, Any] = {}
    exists = folder.is_dir()
    if (folder / "brand.yaml").is_file():
        try:
            cfg = yaml.safe_load((folder / "brand.yaml").read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            cfg = {}
    colors = dict(NEUTRAL_COLORS)
    for k, v in (cfg.get("colors") or {}).items():
        if k in colors:
            colors[k] = _color(v, colors[k])
    deck = cfg.get("deck") or {}
    b = Brand(
        node=node,
        folder=folder if exists else None,
        company=str(cfg.get("company") or "").strip(),
        side_label=str(cfg.get("side_label") or "").strip(),
        footer=str(cfg.get("footer") or "Confidencial").strip(),
        prepared_by=str(cfg.get("prepared_by") or "Elaborado con ATLAS").strip(),
        colors=colors,
        deck_font=str(deck.get("font") or "Segoe UI Light"),
        deck_font_strong=str(deck.get("font_strong") or "Segoe UI Semibold"),
        pdf_fonts=resolve_pdf_fonts(folder if exists else None, cfg.get("pdf_fonts") or {}),
        configured=bool(cfg),
    )
    if exists:
        b.logo_light = _file(folder, cfg.get("logo_light"), "logo_light.png")
        b.logo_dark = _file(folder, cfg.get("logo_dark"), "logo_dark.png")
        b.deck_base = _file(folder, deck.get("base"), "deck_base.pptx")
    return b


# ---------------------------------------------------------------------------
# Building a kit from the company's PowerPoint template
# ---------------------------------------------------------------------------

_TEMPLATE_TYPES = (
    "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
)
_PRESENTATION_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def deck_base_from_template(template: Path, out: Path) -> Path:
    """A .pptx with the template's theme, masters, layouts and slide size, and no slides (.potx/.potm/.pptx/.pptm
    accepted; macros are dropped)."""
    import io
    import zipfile

    from pptx import Presentation

    buf = io.BytesIO()
    with zipfile.ZipFile(template) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                for t in _TEMPLATE_TYPES:
                    text = text.replace(t, _PRESENTATION_TYPE)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    buf.seek(0)
    prs = Presentation(buf)
    for rid, rel in list(prs.part.rels.items()):
        if "vbaProject" in rel.reltype:  # macros cannot live in a .pptx
            prs.part.drop_rel(rid)
    ids = prs.slides._sldIdLst
    for sld in list(ids):
        prs.part.drop_rel(sld.rId)
        ids.remove(sld)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out
