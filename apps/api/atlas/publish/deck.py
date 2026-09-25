"""Committee deck (python-pptx) in the company's visual language.

Built on the brand kit's deck_base.pptx (the company template without its slides: theme, size, fonts) using its
blank layout, and drawn with shapes: full-color cover and closing, section slides with a circle accent, light
content slides with a vertical side label, color bands for KPIs, branded tables and native charts (editable in
PowerPoint). Coordinates are in a 1920 x 1080 grid scaled to the template's slide size."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

from .brand import Brand
from .pdf import col_weights, fmt_num, is_numeric
from .spec import accent_runs, plain


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
        if text:
            self.text(s, 120, 1010, 1500, 30, "Fuente: " + text, size=15,
                      color=_mix(self.b.c("primary"), "#FFFFFF", 0.6) if dark else self.b.c("muted"))

    def title(self, s: Any, title: str, subtitle: str = "", *, y: float = 90, color: str | None = None) -> None:
        self.text(s, 120, y, 1560, 120, title, size=54, color=color or self.b.c("primary"), accents=True)
        if subtitle:
            self.text(s, 120, y + 118, 1560, 60, subtitle, size=24, color=self.b.c("muted"))

    def logo(self, s: Any, path: Path | None, x: float, y: float, h: float) -> bool:
        if not path:
            return False
        try:
            s.shapes.add_picture(str(path), self.e(x), self.e(y), height=self.e(h))
            return True
        except Exception:  # noqa: BLE001 — a broken logo never breaks the deck
            return False

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
        self.text(s, 120, 600, 1500, 250, spec["title"], size=76, color="#FFFFFF", accents=True, line=0.95)
        if spec.get("subtitle"):
            self.text(s, 120, 860, 1500, 80, spec["subtitle"], size=26, color=light)
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
        size = 38 if len(items) <= 4 else 33 if len(items) <= 6 else 29
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
        size = 22 if row_h >= 64 else 19 if row_h >= 52 else 16
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
        w = 1680 / n
        light = _mix(b.c("primary"), "#FFFFFF", 0.62)
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
        self.title(s, sl.get("title") or ch.get("title") or "", sl.get("subtitle", ""))
        data = CategoryChartData()
        data.categories = ch["categories"]
        for sr in ch["series"]:
            data.add_series(sr["name"], sr["values"])
        kind = XL_CHART_TYPE.LINE_MARKERS if ch["chart_type"] == "line" else XL_CHART_TYPE.COLUMN_CLUSTERED
        top = 300 if sl.get("subtitle") else 250
        gf = s.shapes.add_chart(kind, self.e(120), self.e(top), self.e(1680), self.e(960 - top), data)
        c = gf.chart
        pal = [b.c("primary"), b.c("accent"), _mix(b.c("primary"), "#FFFFFF", 0.45), b.c("muted")]
        c.has_title = False
        c.font.size = self.pt(20)
        c.font.name = self.font
        c.font.color.rgb = _rgb(b.c("ink"))
        c.has_legend = len(ch["series"]) > 1
        if c.has_legend:
            c.legend.position = XL_LEGEND_POSITION.TOP
            c.legend.include_in_layout = False
        for i, series in enumerate(c.series):
            color = _rgb(pal[i % len(pal)])
            if ch["chart_type"] == "line":
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.25)
                series.smooth = False
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = color
                series.marker.format.line.color.rgb = color
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
        va = c.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _rgb(b.c("rule"))
        va.format.line.fill.background()
        va.tick_labels.font.color.rgb = _rgb(b.c("muted"))
        va.tick_labels.number_format = "#,##0.##" if any(
            v is not None and not float(v).is_integer() for sr in ch["series"] for v in sr["values"]) else "#,##0"
        va.tick_labels.number_format_is_linked = False
        ca = c.category_axis
        ca.format.line.color.rgb = _rgb(b.c("rule"))
        ca.tick_labels.font.color.rgb = _rgb(b.c("ink"))
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

    def build(self, spec: dict[str, Any], closing: list[str], out: Path) -> Path:
        self.cover(spec)
        for sl in spec["slides"]:
            getattr(self, sl["type"])(sl)
        self.closing(closing)
        self.prs.core_properties.title = plain(spec["title"])
        self.prs.core_properties.author = self.b.company or "ATLAS"
        self.prs.save(str(out))
        return out


def render_deck(spec: dict[str, Any], brand: Brand, date_text: str, closing: list[str], out: Path) -> Path:
    return Deck(brand, date_text).build(spec, closing, out)
