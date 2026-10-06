"""Committee deck (python-pptx) in the company's visual language.

Built on the brand kit's deck_base.pptx (the company template without its slides: theme, size, fonts) using its
blank layout, and drawn with shapes: full-color cover and closing, section slides with a circle accent, light
content slides with a vertical side label, color bands for KPIs, branded tables and native charts (editable in
PowerPoint). Coordinates are in a 1920 x 1080 grid scaled to the template's slide size."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_MARKER_STYLE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

from .brand import Brand
from .pdf import col_weights, fmt_num, is_numeric
from .spec import (
    DEFAULT_STYLE,
    accent_runs,
    image_file,
    is_capture,
    nice_bounds,
    normalize_style,
    num_format,
    own_group,
    plain,
    say_number,
)


def _rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_.lstrip("#").upper())


_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _borders(cell: Any, color: str, *, bottom_only: bool = False) -> None:
    """Hairline cell borders (only the bottom one drawn), instead of the viewer's default grid."""
    tcPr = cell._tc.get_or_add_tcPr()
    for i, tag in enumerate(("lnL", "lnR", "lnT", "lnB")):  # schema order: borders before the fill
        for old in tcPr.findall(_A + tag):
            tcPr.remove(old)
        draw = tag == "lnB" or not bottom_only
        ln = tcPr.makeelement(_A + tag, {"w": "6350" if draw else "0", "cmpd": "sng"})
        if draw:
            fill = ln.makeelement(_A + "solidFill", {})
            fill.append(fill.makeelement(_A + "srgbClr", {"val": color.lstrip("#")}))
            ln.append(fill)
        else:
            ln.append(ln.makeelement(_A + "noFill", {}))
        tcPr.insert(i, ln)


def _fmt_ref(v: float, unit: str = "") -> str:
    return f"{say_number(v)} {unit}".strip()


def _ref_label(ref: dict[str, Any], unit: str = "") -> str:
    """'Promedio comparables: 88,491 MXN/m²' — the label alone when it already says the value."""
    label = ref.get("label") or "Referencia"
    return label if re.search(r"\$|\d[\d,.]{2,}", label) else f"{label}: {_fmt_ref(ref['value'], unit)}"


def _mix(a: str, b: str, t: float) -> str:
    x, y = a.lstrip("#"), b.lstrip("#")
    ch = [round(int(x[i:i + 2], 16) + (int(y[i:i + 2], 16) - int(x[i:i + 2], 16)) * t) for i in (0, 2, 4)]
    return "#" + "".join(f"{c:02X}" for c in ch)


class Deck:
    def __init__(self, brand: Brand, date_text: str):
        self.b = brand
        self.date_text = date_text
        self.prs = Presentation(str(brand.deck_base)) if brand.deck_base else Presentation()
        if not brand.deck_base:
            self.prs.slide_width, self.prs.slide_height = Emu(12192000), Emu(6858000)
        ids = self.prs.slides._sldIdLst
        for sld in list(ids):
            self.prs.part.drop_rel(sld.rId)
            ids.remove(sld)
        self.k = self.prs.slide_width / 1920  # EMU per grid unit
        layouts = list(self.prs.slide_layouts)
        blank = [lay for lay in layouts if lay.name.lower() in ("en blanco", "blank")]
        self.layout = blank[0] if blank else min(layouts, key=lambda lay: len(lay.placeholders))
        self.style = dict(DEFAULT_STYLE)
        self.scale = 1.0
        self.font = brand.deck_font
        self.strong = brand.deck_font_strong
        self.n = 0

    # -- primitives ------------------------------------------------------------

    def e(self, v: float) -> Emu:
        return Emu(int(v * self.k))

    def pt(self, size: float) -> Pt:
        """Font size given for a 1920-wide slide (13.33 in = 20 pt per 1/96 in grid...) scaled to the deck."""
        return Pt(size * self.prs.slide_width / 24384000)

    def new(self, bg: str | None = None) -> Any:
        s = self.prs.slides.add_slide(self.layout)
        for ph in list(s.placeholders):
            ph._element.getparent().remove(ph._element)
        if bg:
            s.background.fill.solid()
            s.background.fill.fore_color.rgb = _rgb(bg)
        self.n += 1
        return s

    def rect(self, s: Any, x: float, y: float, w: float, h: float, color: str, shape: Any = MSO_SHAPE.RECTANGLE) -> Any:
        r = s.shapes.add_shape(shape, self.e(x), self.e(y), self.e(w), self.e(h))
        r.fill.solid()
        r.fill.fore_color.rgb = _rgb(color)
        r.line.fill.background()
        r.shadow.inherit = False
        return r

    def text(self, s: Any, x: float, y: float, w: float, h: float, text: str | list[tuple[str, bool]], *,
             size: float = 28, color: str = "#000000", font: str | None = None, align: Any = PP_ALIGN.LEFT,
             anchor: Any = MSO_ANCHOR.TOP, accents: bool = False, spacing: float | None = None,
             line: float | None = None) -> Any:
        box = s.shapes.add_textbox(self.e(x), self.e(y), self.e(w), self.e(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        paras = text if isinstance(text, list) else [text]
        first = True
        for para in paras:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.alignment = align
            if line:
                p.line_spacing = line
            runs = accent_runs(para) if accents and isinstance(para, str) else \
                [(plain(para) if isinstance(para, str) else para[0], False)]
            for chunk, italic in runs:
                r = p.add_run()
                r.text = chunk
                r.font.size = self.pt(size)
                r.font.name = font or self.font
                r.font.italic = italic
                r.font.color.rgb = _rgb(color)
                if spacing is not None:
                    r.font._element.set("spc", str(int(spacing * 100)))
        return box

    def side_label(self, s: Any, dark: bool = False) -> None:
        if not self.b.label:
            return
        box = self.text(s, -330, 540, 760, 30, f"{self.b.label}  |  {self.date_text[-4:]}", size=13,
                        color=_mix(self.b.c("primary"), "#FFFFFF", 0.55) if dark else self.b.c("muted"),
                        spacing=2)
        box.rotation = 270

    def page_no(self, s: Any, dark: bool = False) -> None:
        self.text(s, 1780, 1010, 80, 30, f"{self.n:02d}", size=13,
                  color="#FFFFFF" if dark else self.b.c("primary"), font=self.strong, align=PP_ALIGN.RIGHT)

    def source(self, s: Any, text: str, dark: bool = False) -> None:
        text = re.sub(r"^(fuente\s*:\s*)+", "", text or "", flags=re.IGNORECASE).strip()
        if text:
            self.text(s, 120, 1010, 1500, 30, "Fuente: " + text, size=15,
                      color=_mix(self.b.c("primary"), "#FFFFFF", 0.6) if dark else self.b.c("muted"))

    def title(self, s: Any, title: str, subtitle: str = "", *, y: float = 90, color: str | None = None) -> float:
        """Draw the title (an action title can take two lines) and subtitle; returns where content may start."""
        long = len(plain(title)) > 50
        size = 42 if long else 54
        lines = 2 if len(plain(title)) > (66 if long else 50) else 1
        h = 62 * lines if long else 120
        self.text(s, 120, y, 1560, h, title, size=size, color=color or self.b.c("primary"), accents=True)
        bottom = y + (h + 14 if lines == 2 else 118)
        if subtitle:
            self.text(s, 120, bottom, 1560, 60, subtitle, size=24, color=self.b.c("muted"))
            bottom += 70
        return bottom

    def logo(self, s: Any, path: Path | None, x: float, y: float, h: float) -> bool:
        if not path:
            return False
        try:
            s.shapes.add_picture(str(path), self.e(x), self.e(y), height=self.e(h))
            return True
        except Exception:  # noqa: BLE001 — a broken logo never breaks the deck
            return False

    def picture(self, s: Any, path: Path | None, x: float, y: float, w: float, h: float, *,
                contain: bool = False) -> bool:
        """The image filling the box (cropped to its proportions, like 'fill' in PowerPoint), or with `contain`
        whole and centered in it (screenshots and charts: nothing may be cut off or stretched)."""
        if not path:
            return False
        try:
            from PIL import Image

            with Image.open(path) as im:
                iw, ih = im.size
            box, img = w / h, iw / ih
            if contain:
                fw, fh = (w, w / img) if img > box else (h * img, h)
                pic = s.shapes.add_picture(str(path), self.e(x + (w - fw) / 2), self.e(y + (h - fh) / 2),
                                           self.e(fw), self.e(fh))
                pic.line.color.rgb = _rgb(self.b.c("rule"))
                pic.line.width = Pt(0.75)
                return True
            pic = s.shapes.add_picture(str(path), self.e(x), self.e(y), self.e(w), self.e(h))
            if img > box:  # wider than the box: crop the sides
                cut = (1 - box / img) / 2
                pic.crop_left = pic.crop_right = cut
            elif img < box:  # taller: crop top and bottom
                cut = (1 - img / box) / 2
                pic.crop_top = pic.crop_bottom = cut
            return True
        except Exception:  # noqa: BLE001 — a bad image never breaks the deck
            return False

    def missing_image(self, s: Any, x: float, y: float, w: float, h: float) -> None:
        self.rect(s, x, y, w, h, _mix(self.b.c("primary"), "#FFFFFF", 0.88))
        self.text(s, x, y + h / 2 - 20, w, 40, "Imagen no disponible", size=18, color=self.b.c("muted"),
                  align=PP_ALIGN.CENTER)

    def image(self, sl: dict[str, Any]) -> None:
        b = self.b
        path = image_file(sl.get("image"))
        if sl.get("layout") == "fit" or (path is not None and is_capture(path)):
            # evidence: a screenshot or chart shown whole, under its title, with its source
            s = self.new("#FFFFFF")
            top = self.title(s, sl.get("title") or "", sl.get("subtitle", ""))
            top = max(top + 10, 230)
            items = sl.get("items") or []
            width = 1680 if not items else 1180
            if not self.picture(s, path, 120, top, width, 975 - top, contain=True):
                self.missing_image(s, 120, top, width, 975 - top)
            if items:
                self.text(s, 1360, top, 440, 975 - top, [f"▪  {i}" for i in items], size=21 * self.scale,
                          color=b.c("ink"), line=1.15)
            self.source(s, sl.get("source") or sl.get("caption", ""))
            self.side_label(s)
            self.page_no(s)
        elif sl.get("layout") == "full":
            s = self.new(b.c("dark"))
            if not self.picture(s, path, 0, 0, 1920, 1080):
                self.missing_image(s, 0, 0, 1920, 1080)
            if sl.get("title"):
                self.rect(s, 0, 850, 1920, 230, b.c("primary"))
                self.text(s, 120, 880, 1500, 110, sl["title"], size=46, color="#FFFFFF", accents=True)
                if sl.get("subtitle") or sl.get("caption"):
                    self.text(s, 120, 985, 1500, 50, sl.get("subtitle") or sl.get("caption"), size=20,
                              color=_mix(b.c("primary"), "#FFFFFF", 0.62))
            self.page_no(s, dark=True)
        else:
            s = self.new("#FFFFFF")
            if not self.picture(s, path, 820, 0, 1100, 1080):
                self.missing_image(s, 820, 0, 1100, 1080)
            self.rect(s, 120, 330, 6, 90, b.c("primary"))
            self.text(s, 120, 110, 640, 220, sl.get("title") or "", size=50, color=b.c("primary"), accents=True,
                      anchor=MSO_ANCHOR.BOTTOM)
            if sl.get("subtitle"):
                self.text(s, 150, 335, 610, 90, sl["subtitle"], size=22, color=b.c("muted"))
            if sl.get("items"):
                self.text(s, 120, 470, 640, 440, [f"▪  {i}" for i in sl["items"]], size=26 * self.scale,
                          color=b.c("ink"), line=1.15)
            if sl.get("caption"):
                self.text(s, 120, 950, 640, 40, sl["caption"], size=15, color=b.c("muted"))
            self.side_label(s)
            self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def notes(self, s: Any, text: str) -> None:
        if text:
            s.notes_slide.notes_text_frame.text = text

    # -- slides ----------------------------------------------------------------

    def cover(self, spec: dict[str, Any]) -> None:
        b = self.b
        s = self.new(b.c("primary"))
        light = _mix(b.c("primary"), "#FFFFFF", 0.62)
        if not self.logo(s, b.logo_light, 120, 110, 190):
            self.text(s, 120, 140, 900, 60, (b.company or "ATLAS").upper(), size=30, color="#FFFFFF",
                      font=self.strong, spacing=6)
        self.rect(s, 120, 520, 70, 3, "#FFFFFF")
        if spec.get("doc_kind"):
            self.text(s, 120, 550, 1400, 40, spec["doc_kind"].upper(), size=17, color=light, spacing=5)
        photo = image_file(spec.get("cover_image"))
        wide = 1500
        if photo and self.picture(s, photo, 1160, 0, 760, 1080):
            wide = 980  # the photo takes the right third
        self.text(s, 120, 600, wide, 250, spec["title"], size=76 if wide > 1000 else 64, color="#FFFFFF",
                  accents=True, line=0.95)
        if spec.get("subtitle"):
            self.text(s, 120, 860, wide, 80, spec["subtitle"], size=26, color=light)
        self.text(s, 120, 985, 900, 40, self.date_text, size=16, color="#FFFFFF")
        self.text(s, 1000, 985, 800, 40, b.footer, size=13, color=light, align=PP_ALIGN.RIGHT)

    def section(self, sl: dict[str, Any]) -> None:
        b = self.b
        s = self.new(b.c("dark"))
        self.rect(s, 1210, 290, 500, 500, b.c("primary"), MSO_SHAPE.OVAL)
        self.text(s, 780, 430, 900, 230, sl["title"], size=64, color="#FFFFFF", accents=True,
                  align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE, line=0.95)
        if sl.get("subtitle"):
            self.text(s, 780, 690, 900, 60, sl["subtitle"], size=22, color=_mix(b.c("primary"), "#FFFFFF", 0.6),
                      align=PP_ALIGN.RIGHT)
        self.side_label(s, dark=True)
        self.page_no(s, dark=True)
        self.notes(s, sl.get("notes", ""))

    def bullets(self, sl: dict[str, Any]) -> None:
        b = self.b
        s = self.new(b.c("light"))
        self.rect(s, 120, 330, 6, 560, b.c("primary"))
        self.text(s, 120, 110, 1560, 150, sl["title"], size=58, color=b.c("primary"), accents=True)
        if sl.get("subtitle"):
            self.text(s, 120, 238, 1560, 60, sl["subtitle"], size=24, color=b.c("muted"))
        items = sl["items"]
        size = (38 if len(items) <= 4 else 33 if len(items) <= 6 else 29) * self.scale
        box = self.text(s, 180, 330, 1560, 620, [i.lstrip("\t") for i in items], size=size, color=b.c("ink"),
                        line=1.1)
        for p, item in zip(box.text_frame.paragraphs, items, strict=True):
            sub = item.startswith("\t")  # a detail under the previous bullet
            p.space_after = self.pt(size * (0.35 if sub else 0.75))
            if sub:
                for r in p.runs:
                    r.font.size = self.pt(size * 0.82)
                    r.font.color.rgb = _rgb(b.c("muted"))
            pPr = p._p.get_or_add_pPr()
            pPr.set("marL", str(int(self.e(90 if sub else 34))))
            pPr.set("indent", str(-int(self.e(34))))
            bu = pPr.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}buChar",
                                 {"char": "–" if sub else "▪"})
            clr = pPr.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}buClr", {})
            srgb = clr.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}srgbClr",
                                   {"val": b.c("primary").lstrip("#")})
            clr.append(srgb)
            pPr.append(clr)
            pPr.append(bu)
        self.side_label(s)
        self.source(s, sl.get("source", ""))
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def table(self, sl: dict[str, Any]) -> None:
        b = self.b
        t = sl["table"]
        s = self.new("#FFFFFF")
        self.title(s, sl.get("title") or t.get("caption") or "", sl.get("subtitle", ""))
        cols, rows = t["columns"], t["rows"]
        top = 300 if sl.get("subtitle") else 250
        row_h = min(78, (930 - top) / (len(rows) + 1))
        size = (22 if row_h >= 64 else 19 if row_h >= 52 else 16) * min(self.scale, 1.06)
        shape = s.shapes.add_table(len(rows) + 1, len(cols), self.e(120), self.e(top), self.e(1680),
                                   self.e(row_h * (len(rows) + 1)))
        tbl = shape.table
        tblPr = tbl._tbl.tblPr
        for child in list(tblPr):
            if child.tag.endswith("tableStyleId"):
                tblPr.remove(child)
        tbl.first_row = True
        tbl.horz_banding = False
        numeric = [all(is_numeric(r[i]) or r[i] in ("", "-", "—") for r in rows) and
                   any(is_numeric(r[i]) for r in rows) for i in range(len(cols))]
        for i, w in enumerate(col_weights(cols, rows)):
            tbl.columns[i].width = self.e(1680 * w)
        for r_i in range(len(rows) + 1):
            tbl.rows[r_i].height = self.e(row_h)
            for c_i in range(len(cols)):
                cell = tbl.cell(r_i, c_i)
                header = r_i == 0
                value = cols[c_i] if header else fmt_num(rows[r_i - 1][c_i])
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(b.c("primary") if header else
                                                (b.c("light") if r_i % 2 == 0 else "#FFFFFF"))
                cell.margin_left = cell.margin_right = self.e(14)
                cell.margin_top = cell.margin_bottom = self.e(4)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf = cell.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                p.alignment = PP_ALIGN.RIGHT if numeric[c_i] else PP_ALIGN.LEFT
                run = p.add_run()
                run.text = str(value)
                run.font.size = self.pt(size)
                run.font.name = self.strong if header else self.font
                run.font.color.rgb = _rgb("#FFFFFF" if header else b.c("ink"))
                _borders(cell, b.c("primary") if r_i == len(rows) else b.c("rule"), bottom_only=True)
        self.side_label(s)
        self.source(s, t.get("source") or sl.get("source", ""))
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def kpis(self, sl: dict[str, Any]) -> None:
        b = self.b
        s = self.new(b.c("light"))
        self.rect(s, 0, 540, 1920, 540, b.c("primary"))
        self.title(s, sl.get("title", ""), sl.get("subtitle", ""), y=150)
        kp = sl["kpis"]
        n = len(kp)
        light = _mix(b.c("primary"), "#FFFFFF", 0.62)
        if n > 4:  # "the market on one page": two rows of three, compact
            self.rect(s, 0, 380, 1920, 700, b.c("primary"))
            per = 3
            w = 1680 / per
            for i, k in enumerate(kp):
                x, y = 120 + (i % per) * w, 430 + (i // per) * 320
                self.rect(s, x, y, 60, 3, "#FFFFFF")
                self.text(s, x, y + 22, w - 40, 110, k["value"], size=56, color="#FFFFFF")
                self.text(s, x, y + 140, w - 40, 36, k["label"].upper(), size=17, color=light, spacing=2)
                if k.get("note"):
                    self.text(s, x, y + 180, w - 40, 80, k["note"], size=17, color=light)
        else:
            w = 1680 / n
            for i, k in enumerate(kp):
                x = 120 + i * w
                self.rect(s, x, 640, 60, 3, "#FFFFFF")
                self.text(s, x, 670, w - 40, 130, k["value"], size=72 if n <= 3 else 60, color="#FFFFFF")
                self.text(s, x, 815, w - 40, 40, k["label"].upper(), size=19, color=light, spacing=2)
                if k.get("note"):
                    self.text(s, x, 860, w - 40, 90, k["note"], size=19, color=light)
        self.side_label(s)
        self.source(s, sl.get("source", ""), dark=True)
        self.page_no(s, dark=True)
        self.notes(s, sl.get("notes", ""))

    def chart(self, sl: dict[str, Any]) -> None:
        b = self.b
        ch = sl["chart"]
        s = self.new("#FFFFFF")
        content_top = self.title(s, sl.get("title") or ch.get("title") or "", sl.get("subtitle", ""))
        if ch["chart_type"] == "scatter":
            self.scatter(s, sl, ch)
            return
        self.source(s, ch.get("source") or sl.get("source", ""))
        self.side_label(s)
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))
        horizontal = ch["chart_type"] == "bar" and ch.get("orientation") == "horizontal"
        ref = ch.get("reference")
        data = CategoryChartData()
        data.categories = ch["categories"]
        for sr in ch["series"]:
            data.add_series(sr["name"], sr["values"])
        if ref and ch["chart_type"] == "line":  # the benchmark as a flat dashed series
            data.add_series(ref.get("label") or "Referencia", [ref["value"]] * len(ch["categories"]))
        kind = (XL_CHART_TYPE.LINE_MARKERS if ch["chart_type"] == "line"
                else XL_CHART_TYPE.BAR_CLUSTERED if horizontal else XL_CHART_TYPE.COLUMN_CLUSTERED)
        top = max(300 if sl.get("subtitle") else 250, content_top + 20)
        if ref and ch["chart_type"] == "bar":
            label = _ref_label(ref, ch.get("unit", ""))
            self.text(s, 1100, top - 50, 700, 40, label, size=20, color=b.c("muted"), align=PP_ALIGN.RIGHT)
        gf = s.shapes.add_chart(kind, self.e(120), self.e(top), self.e(1680), self.e(960 - top), data)
        c = gf.chart
        pal = [b.c("primary"), b.c("accent"), _mix(b.c("primary"), "#FFFFFF", 0.45), b.c("muted")]
        c.has_title = False
        c.font.size = self.pt(16 if horizontal and len(ch["categories"]) > 12 else 20)
        c.font.name = self.font
        c.font.color.rgb = _rgb(b.c("ink"))
        sparse = ch["chart_type"] == "line" and len(ch["categories"]) > 8
        if sparse and self.style.get("chart_data_labels"):  # a label on every point is unreadable: last ones only
            for sr, series in zip(ch["series"], c.series, strict=False):
                last = max((j for j, v in enumerate(sr["values"]) if v is not None), default=None)
                if last is None:
                    continue
                dl = series.points[last].data_label
                dl.has_text_frame = True
                dl.text_frame.text = say_number(sr["values"][last], [v for x in ch["series"] for v in x["values"]])
                dl.position = XL_LABEL_POSITION.RIGHT
                dl.font.size = self.pt(15)
                dl.font.color.rgb = _rgb(b.c("ink"))
        elif self.style.get("chart_data_labels") or horizontal:
            plot = c.plots[0]
            plot.has_data_labels = True
            labels = plot.data_labels
            labels.font.size = self.pt(15)
            labels.font.color.rgb = _rgb(b.c("ink"))
            labels.number_format = num_format([v for sr in ch["series"] for v in sr["values"]])[0]
            labels.number_format_is_linked = False
        c.has_legend = len(c.series) > 1
        if c.has_legend:
            c.legend.position = XL_LEGEND_POSITION.TOP
            c.legend.include_in_layout = False
        for i, series in enumerate(c.series):
            color = _rgb(pal[i % len(pal)])
            if ch["chart_type"] == "line" and ref and i == len(c.series) - 1:
                series.format.line.color.rgb = _rgb(b.c("muted"))
                series.format.line.width = Pt(1.75)
                series.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
                series.smooth = False
                series.marker.style = XL_MARKER_STYLE.NONE
            elif ch["chart_type"] == "line":
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.25)
                series.smooth = False
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = color
                series.marker.format.line.color.rgb = color
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
                if ch.get("highlight") and len(ch["series"]) == 1:
                    # the project in the full primary color, the rest of the market lighter
                    series.format.fill.fore_color.rgb = _rgb(_mix(b.c("primary"), "#FFFFFF", 0.6))
                    lit = set(ch["highlight"])
                    for j, cat in enumerate(ch["categories"]):
                        if cat in lit:
                            pt = series.points[j]
                            pt.format.fill.solid()
                            pt.format.fill.fore_color.rgb = color
        if horizontal:
            c.category_axis.reverse_order = True  # first category on top: rankings read top-down
            c.plots[0].gap_width = 40
        va = c.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _rgb(b.c("rule"))
        va.format.line.fill.background()
        va.tick_labels.font.color.rgb = _rgb(b.c("muted"))
        va.tick_labels.number_format = num_format([v for sr in ch["series"] for v in sr["values"]])[0]
        va.tick_labels.number_format_is_linked = False
        values = [v for sr in ch["series"] for v in sr["values"] if v is not None]
        if ref:
            values.append(ref["value"])
        if ch["chart_type"] == "line" and values and min(values) > 0 and min(values) > 0.3 * max(values):
            lo, hi, step = nice_bounds(min(values), max(values))  # a price series doesn't start at zero
            va.minimum_scale, va.maximum_scale, va.major_unit = lo, hi, step
        if horizontal:  # the values are on the bars
            va.visible = False
            va.has_major_gridlines = False
        ca = c.category_axis
        ca.format.line.color.rgb = _rgb(b.c("rule"))
        ca.tick_labels.font.color.rgb = _rgb(b.c("ink"))
        if len(ch["categories"]) > 12 and not horizontal:  # every other label: 23 quarters can't all fit
            from pptx.oxml.ns import qn

            ax = ca._element
            for tag in ("c:tickLblSkip", "c:tickMarkSkip"):
                old = ax.find(qn(tag))
                if old is not None:
                    ax.remove(old)
            skip = ax.makeelement(qn("c:tickLblSkip"), {"val": "2"})
            no_mult = ax.find(qn("c:noMultiLvlLbl"))
            (no_mult.addprevious if no_mult is not None else ax.append)(skip)

    def scatter(self, s: Any, sl: dict[str, Any], ch: dict[str, Any]) -> None:
        """Positioning chart: every point labeled (its project), one color per group, both axes titled."""
        b = self.b
        data = XyChartData()
        longest = max(len(sr["points"]) for sr in ch["series"])
        for sr in ch["series"]:
            xs = data.add_series(sr["name"])
            for p in sr["points"]:
                xs.add_data_point(p["x"], p["y"])
            # LibreOffice hides a series that has fewer points than a later one: pad it by repeating its last
            # point (same spot, no label), so every viewer draws every group
            for _ in range(longest - len(sr["points"])):
                xs.add_data_point(sr["points"][-1]["x"], sr["points"][-1]["y"])
        top = 300 if sl.get("subtitle") else 250
        gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, self.e(120), self.e(top), self.e(1680),
                                self.e(960 - top), data)
        c = gf.chart
        pal = [b.c("primary"), b.c("accent"), _mix(b.c("primary"), "#FFFFFF", 0.45), b.c("muted")]
        c.has_title = False
        c.font.size = self.pt(18)
        c.font.name = self.font
        c.font.color.rgb = _rgb(b.c("ink"))
        c.has_legend = len(ch["series"]) > 1
        if c.has_legend:
            c.legend.position = XL_LEGEND_POSITION.TOP
            c.legend.include_in_layout = False
        own = own_group(ch)
        if own is not None:  # the project in the primary color, the market lighter
            others = [_mix(b.c("primary"), "#FFFFFF", 0.55), b.c("muted"), b.c("accent")]
            pal = [b.c("primary") if i == own else others[(i - (i > own)) % len(others)]
                   for i in range(len(ch["series"]))]
        for i, (series, sr) in enumerate(zip(c.series, ch["series"], strict=False)):
            color = _rgb(pal[i % len(pal)])
            series.format.line.fill.background()  # points only, never joined
            series.marker.style = XL_MARKER_STYLE.CIRCLE
            series.marker.size = 13
            series.marker.format.fill.solid()
            series.marker.format.fill.fore_color.rgb = color
            series.marker.format.line.color.rgb = _rgb("#FFFFFF")
            for j, p in enumerate(sr["points"]):
                if not p["label"]:
                    continue
                dl = series.points[j].data_label
                dl.has_text_frame = True
                dl.text_frame.text = p["label"]
                dl.position = XL_LABEL_POSITION.RIGHT
                run = dl.text_frame.paragraphs[0].runs[0]
                run.font.size = self.pt(20)
                run.font.color.rgb = _rgb(b.c("ink"))
        xs = [p["x"] for sr in ch["series"] for p in sr["points"]]
        ys = [p["y"] for sr in ch["series"] for p in sr["points"]]
        for axis, vals, title in ((c.category_axis, xs, ch.get("x_title")), (c.value_axis, ys, ch.get("y_title"))):
            axis.has_major_gridlines = axis is c.value_axis
            if axis.has_major_gridlines:
                axis.major_gridlines.format.line.color.rgb = _rgb(b.c("rule"))
            axis.format.line.color.rgb = _rgb(b.c("rule"))
            axis.tick_labels.font.color.rgb = _rgb(b.c("muted"))
            axis.tick_labels.number_format = "#,##0.##" if any(not float(v).is_integer() for v in vals) else "#,##0"
            axis.tick_labels.number_format_is_linked = False
            lo, hi, step = nice_bounds(min(vals), max(vals))
            axis.minimum_scale, axis.maximum_scale, axis.major_unit = lo, hi, step
            if title:
                axis.has_title = True
                axis.axis_title.text_frame.text = title
                r = axis.axis_title.text_frame.paragraphs[0].runs[0]
                r.font.size = self.pt(18)
                r.font.bold = False
                r.font.color.rgb = _rgb(b.c("muted"))
        if ch.get("unit"):
            self.text(s, 120, top - 36, 600, 30, ch["unit"], size=13, color=b.c("muted"))
        self.side_label(s)
        self.source(s, ch.get("source") or sl.get("source", ""))
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def statement(self, sl: dict[str, Any]) -> None:
        b = self.b
        s = self.new("#FFFFFF")
        self.rect(s, 0, 0, 640, 1080, b.c("primary"))
        self.rect(s, 760, 360, 4, 360, b.c("primary"))
        self.text(s, 810, 330, 980, 420, sl["text"], size=58, color=b.c("primary"), accents=True,
                  anchor=MSO_ANCHOR.MIDDLE, line=1.15)
        if sl.get("title"):
            self.text(s, 120, 470, 440, 140, sl["title"], size=40, color="#FFFFFF", accents=True)
        self.source(s, sl.get("source", ""))
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def quote(self, sl: dict[str, Any]) -> None:
        b = self.b
        s = self.new(b.c("light"))
        self.text(s, 300, 230, 200, 200, "“", size=220, color=b.c("accent"))
        self.text(s, 420, 360, 1200, 360, sl["text"], size=54, color=b.c("primary"), accents=True, line=1.15)
        if sl.get("attribution"):
            self.text(s, 420, 760, 1200, 50, "— " + sl["attribution"], size=20, color=b.c("muted"))
        self.side_label(s)
        self.page_no(s)
        self.notes(s, sl.get("notes", ""))

    def closing(self, lines: list[str]) -> None:
        b = self.b
        s = self.new(b.c("primary"))
        light = _mix(b.c("primary"), "#FFFFFF", 0.62)
        if not self.logo(s, b.logo_light, 810, 330, 300):
            self.text(s, 360, 470, 1200, 80, (b.company or "ATLAS").upper(), size=44, color="#FFFFFF",
                      font=self.strong, align=PP_ALIGN.CENTER, spacing=8)
        self.text(s, 360, 760, 1200, 150, lines, size=15, color=light, align=PP_ALIGN.CENTER)

    def agenda(self, titles: list[str]) -> None:
        b = self.b
        s = self.new(b.c("light"))
        self.rect(s, 0, 0, 640, 1080, b.c("primary"))
        self.text(s, 120, 440, 440, 140, "*Agenda*", size=64, color="#FFFFFF", accents=True)
        n = len(titles)
        step = min(110, 760 / max(n, 1))
        for i, t in enumerate(titles):
            y = 540 - n * step / 2 + i * step
            self.text(s, 760, y, 120, step, f"{i + 1:02d}", size=30 * self.scale, color=b.c("accent"),
                      font=self.strong)
            self.text(s, 880, y, 900, step, t, size=34 * self.scale, color=b.c("primary"), accents=True)
        self.page_no(s)

    def _expand(self, slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Long tables continue on following slides (style.table_rows_per_slide) instead of being cut."""
        per = int(self.style.get("table_rows_per_slide") or 8)
        out: list[dict[str, Any]] = []
        for sl in slides:
            if sl["type"] != "table" or len(sl["table"]["rows"]) <= per:
                out.append(sl)
                continue
            rows = sl["table"]["rows"]
            for i in range(0, len(rows), per):
                part = {**sl, "table": {**sl["table"], "rows": rows[i:i + per]}}
                if i:
                    part["title"] = (sl.get("title") or sl["table"].get("caption") or "") + " (cont.)"
                    part["notes"] = ""
                out.append(part)
        return out

    def build(self, spec: dict[str, Any], closing: list[str], out: Path) -> Path:
        self.style = normalize_style(spec.get("style"))
        self.scale = {"airy": 1.12, "standard": 1.0, "compact": 0.88}[self.style["deck_density"]]
        self.cover(spec)
        slides = self._expand(spec["slides"])
        sections = [sl["title"] for sl in slides if sl["type"] == "section"]
        if self.style.get("agenda") and sections:
            self.agenda(sections)
        for sl in slides:
            getattr(self, sl["type"])(sl)
        self.closing(closing)
        self.prs.core_properties.title = plain(spec["title"])
        self.prs.core_properties.author = self.b.company or "ATLAS"
        self.prs.save(str(out))
        return out


def render_deck(spec: dict[str, Any], brand: Brand, date_text: str, closing: list[str], out: Path) -> Path:
    return Deck(brand, date_text).build(spec, closing, out)
