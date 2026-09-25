"""Institutional PDF report (reportlab): cover, executive summary, numbered sections, notes & sources.

Look: a full-color cover with the light logo; white interior pages with a thin running header (logo right),
a vertical side label, hairline rules, light-weight headings with an optional italic accent word, tables with
a solid header row and zebra body, KPI tiles, callouts and simple bar / line charts. Colors and fonts come from
the node's brand kit (brand.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .brand import Brand
from .spec import plain

W, H = letter
ML, MR, MT, MB = 70, 58, 70, 64


@dataclass
class DocMeta:
    date_text: str
    notes: dict[str, list[str]] = field(default_factory=dict)  # heading -> lines (assumptions, verification...)
    author_line: str = ""


# ---------------------------------------------------------------------------
# fonts and styles
# ---------------------------------------------------------------------------


def _register(brand: Brand) -> dict[str, str]:
    names: dict[str, str] = {}
    for role, src in brand.pdf_fonts.items():
        if src.lower().endswith((".ttf", ".otf")):
            name = f"ATLAS-{role}-{abs(hash(src)) % 10**8}"
            if name not in pdfmetrics.getRegisteredFontNames():
                try:
                    pdfmetrics.registerFont(TTFont(name, src))
                except Exception:  # noqa: BLE001 — a bad font file falls back to the core font
                    name = {"semibold": "Helvetica-Bold", "italic": "Helvetica-Oblique"}.get(role, "Helvetica")
            names[role] = name
        else:
            names[role] = src
    return names


def _hex(c: str) -> colors.Color:
    return colors.HexColor(c)


def _mix(c: str, other: str, t: float) -> colors.Color:
    a, b = _hex(c), _hex(other)
    return colors.Color(a.red + (b.red - a.red) * t, a.green + (b.green - a.green) * t, a.blue + (b.blue - a.blue) * t)


class Styles:
    def __init__(self, brand: Brand, f: dict[str, str]):
        self.f = f
        ink, prim, muted = _hex(brand.c("ink")), _hex(brand.c("primary")), _hex(brand.c("muted"))
        self.body = ParagraphStyle("body", fontName=f["regular"], fontSize=9.6, leading=14.2, textColor=ink,
                                   spaceAfter=7, alignment=TA_LEFT)
        self.lead = ParagraphStyle("lead", parent=self.body, fontName=f["light"], fontSize=11.5, leading=17,
                                   textColor=ink, spaceAfter=9)
        self.h1 = ParagraphStyle("h1", fontName=f["light"], fontSize=21, leading=26, textColor=prim, spaceAfter=12)
        self.num = ParagraphStyle("num", fontName=f["semibold"], fontSize=7.5, leading=9, textColor=muted,
                                  spaceAfter=3)
        self.caption = ParagraphStyle("caption", fontName=f["semibold"], fontSize=8.2, leading=11, textColor=prim,
                                      spaceBefore=4, spaceAfter=4)
        self.source = ParagraphStyle("source", fontName=f["italic"], fontSize=6.9, leading=9, textColor=muted,
                                     spaceBefore=3, spaceAfter=10)
        self.cell = ParagraphStyle("cell", fontName=f["regular"], fontSize=7.9, leading=10, textColor=ink)
        self.cell_r = ParagraphStyle("cell_r", parent=self.cell, alignment=2)
        self.head = ParagraphStyle("head", fontName=f["semibold"], fontSize=7.4, leading=9.4,
                                   textColor=colors.white)
        self.head_r = ParagraphStyle("head_r", parent=self.head, alignment=2)
        self.kpi_v = ParagraphStyle("kpi_v", fontName=f["light"], fontSize=19, leading=23, textColor=prim)
        self.kpi_l = ParagraphStyle("kpi_l", fontName=f["semibold"], fontSize=6.4, leading=8.5, textColor=muted)
        self.kpi_n = ParagraphStyle("kpi_n", fontName=f["regular"], fontSize=7, leading=9, textColor=ink)
        self.call_t = ParagraphStyle("call_t", fontName=f["semibold"], fontSize=8.6, leading=11, textColor=prim,
                                     spaceAfter=3)
        self.bullet = ParagraphStyle("bullet", parent=self.body, spaceAfter=3.5)


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def rich(text: str, f: dict[str, str], *, accents: bool = False) -> str:
    """Escaped Paragraph markup: **bold** → semibold; with accents, *word* → italic."""
    out = escape(str(text))
    out = _BOLD.sub(lambda m: f'<font name="{f["semibold"]}">{m.group(1)}</font>', out)
    if accents:
        out = re.sub(r"\*([^*]+)\*", lambda m: f'<font name="{f["italic"]}">{m.group(1)}</font>', out)
    else:
        out = out.replace("*", "")
    return out


def fmt_num(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


_NUMERIC = re.compile(r"^[\s$€%()+\-–]*[\d.,]+\s*(%|x|MXN|USD|m²|m2|pp|pb)?[\s)]*$", re.IGNORECASE)


def is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) or bool(_NUMERIC.match(str(v))) and \
        any(ch.isdigit() for ch in str(v))


# ---------------------------------------------------------------------------
# flowables
# ---------------------------------------------------------------------------


class Rule(Flowable):
    def __init__(self, width: float, color: colors.Color, thickness: float = 0.6, space: float = 6):
        super().__init__()
        self.width, self.color, self.thickness, self.space = width, color, thickness, space

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        return self.width, self.space * 2

    def draw(self) -> None:
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.space, self.width, self.space)


def _col_widths(columns: list[str], rows: list[list[Any]], total: float) -> list[float]:
    lens = []
    for i, c in enumerate(columns):
        longest = max([len(str(c))] + [len(fmt_num(r[i])) for r in rows[:40]])
        lens.append(min(max(longest, 5), 38))
    s = sum(lens)
    return [total * n / s for n in lens]


def table_flow(t: dict[str, Any], brand: Brand, st: Styles, width: float) -> list[Any]:
    cols, rows = t["columns"], t["rows"]
    numeric = [all(is_numeric(r[i]) or r[i] in ("", "-", "—") for r in rows) and any(is_numeric(r[i]) for r in rows)
               for i in range(len(cols))]
    data = [[Paragraph(rich(c, st.f), st.head_r if numeric[i] else st.head) for i, c in enumerate(cols)]]
    for r in rows:
        data.append([Paragraph(rich(fmt_num(v), st.f), st.cell_r if numeric[i] else st.cell)
                     for i, v in enumerate(r)])
    tbl = Table(data, colWidths=_col_widths(cols, rows, width), repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _hex(brand.c("primary"))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, _hex(brand.c("primary"))),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), _hex(brand.c("light"))))
        style.append(("LINEBELOW", (0, i), (-1, i), 0.25, _hex(brand.c("rule"))))
    tbl.setStyle(TableStyle(style))
    out: list[Any] = []
    if t.get("caption"):
        out.append(Paragraph(rich(t["caption"], st.f), st.caption))
    out.append(tbl)
    src = t.get("source") or ""
    if t.get("truncated"):
        src = (src + " · " if src else "") + f"{t['truncated']} filas adicionales en el archivo de datos"
    out.append(Paragraph(rich("Fuente: " + src, st.f), st.source) if src else Spacer(1, 10))
    return out


def kpi_flow(kpis: list[dict[str, str]], brand: Brand, st: Styles, width: float) -> Table:
    n = max(1, len(kpis))
    cells = []
    for k in kpis:
        stack = [Paragraph(rich(k["value"], st.f), st.kpi_v),
                 Paragraph(rich(k["label"].upper(), st.f), st.kpi_l)]
        if k.get("note"):
            stack.append(Paragraph(rich(k["note"], st.f), st.kpi_n))
        cells.append(stack)
    gap = 10
    tbl = Table([cells], colWidths=[(width - gap * (n - 1)) / n] * n, hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("RIGHTPADDING", (0, 0), (-1, -1), gap), ("TOPPADDING", (0, 0), (-1, -1), 8),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]
    for i in range(n):
        style.append(("LINEABOVE", (i, 0), (i, 0), 1.4, _hex(brand.c("primary"))))
    tbl.setStyle(TableStyle(style))
    return tbl


def callout_flow(title: str, text: str, brand: Brand, st: Styles, width: float) -> Table:
    inner = ([Paragraph(rich(title, st.f), st.call_t)] if title else []) + \
        [Paragraph(rich(text, st.f), ParagraphStyle("ct", parent=st.body, spaceAfter=0))]
    tbl = Table([[inner]], colWidths=[width], hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _hex(brand.c("light"))),
        ("LINEBEFORE", (0, 0), (0, -1), 2.6, _hex(brand.c("primary"))),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return tbl


def palette(brand: Brand) -> list[colors.Color]:
    return [_hex(brand.c("primary")), _hex(brand.c("accent")), _mix(brand.c("primary"), "#FFFFFF", 0.45),
            _hex(brand.c("muted"))]


def chart_flow(ch: dict[str, Any], brand: Brand, st: Styles, width: float) -> list[Any]:
    h = 200
    d = Drawing(width, h)
    series = ch["series"]
    data = [tuple(v for v in s["values"]) for s in series]
    pal = palette(brand)
    legend_h = 16 if len(series) > 1 else 0
    if ch["chart_type"] == "line":
        c: Any = HorizontalLineChart()
        c.joinedLines = 1
        for i in range(len(series)):
            c.lines[i].strokeColor = pal[i % len(pal)]
            c.lines[i].strokeWidth = 1.8
    else:
        c = VerticalBarChart()
        c.barSpacing = 1
        c.groupSpacing = 8
        for i in range(len(series)):
            c.bars[i].fillColor = pal[i % len(pal)]
            c.bars[i].strokeColor = None
    c.x, c.y, c.width, c.height = 38, 30, width - 48, h - 46 - legend_h
    c.data = [tuple(0 if v is None else v for v in row) for row in data] if ch["chart_type"] == "bar" else data
    c.categoryAxis.categoryNames = ch["categories"]
    c.categoryAxis.labels.fontName = st.f["regular"]
    c.categoryAxis.labels.fontSize = 6.8
    c.categoryAxis.labels.fillColor = _hex(brand.c("ink"))
    if len(ch["categories"]) > 8:
        c.categoryAxis.labels.angle = 35
        c.categoryAxis.labels.boxAnchor = "ne"
    c.categoryAxis.strokeColor = _hex(brand.c("rule"))
    c.valueAxis.labels.fontName = st.f["regular"]
    c.valueAxis.labels.fontSize = 6.8
    c.valueAxis.labels.fillColor = _hex(brand.c("muted"))
    c.valueAxis.labelTextFormat = lambda v: fmt_num(round(v, 2))
    c.valueAxis.strokeColor = None
    c.valueAxis.gridStrokeColor = _hex(brand.c("rule"))
    c.valueAxis.gridStrokeWidth = 0.3
    c.valueAxis.visibleGrid = 1
    values = [v for row in data for v in row if v is not None]
    if values and min(values) >= 0:
        c.valueAxis.valueMin = 0
    d.add(c)
    if len(series) > 1:
        lg = Legend()
        lg.x, lg.y = width - 10, h - 2
        lg.alignment = "right"
        lg.deltax = 90
        lg.fontName, lg.fontSize = st.f["regular"], 7
        lg.colorNamePairs = [(pal[i % len(pal)], s["name"]) for i, s in enumerate(series)]
        lg.boxAnchor = "ne"
        lg.dxTextSpace = 4
        lg.dx = lg.dy = 7
        lg.strokeColor = None
        lg.columnMaximum = 1  # one row: series side by side
        d.add(lg)
    if ch.get("unit"):
        d.add(String(0, h - 8, ch["unit"], fontName=st.f["regular"], fontSize=6.8, fillColor=_hex(brand.c("muted"))))
    out: list[Any] = []
    if ch.get("title"):
        out.append(Paragraph(rich(ch["title"], st.f), st.caption))
    out.append(d)
    out.append(Paragraph(rich("Fuente: " + ch["source"], st.f), st.source) if ch.get("source") else Spacer(1, 10))
    return out


def bullets_flow(items: list[str], brand: Brand, st: Styles) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(rich(i, st.f), st.bullet), leftIndent=12, value="square") for i in items],
        bulletType="bullet", start="square", bulletFontSize=4.2, bulletColor=_hex(brand.c("primary")),
        leftIndent=12, bulletOffsetY=-2.2, spaceAfter=6,
    )


def blocks_flow(blocks: list[dict[str, Any]], brand: Brand, st: Styles, width: float) -> list[Any]:
    out: list[Any] = []
    for b in blocks:
        t = b["type"]
        if t == "paragraph":
            out.append(Paragraph(rich(b["text"], st.f), st.body))
        elif t == "bullets":
            out.append(bullets_flow(b["items"], brand, st))
        elif t == "table":
            out.append(KeepTogether(table_flow(b["table"], brand, st, width)[:3])
                       if len(b["table"]["rows"]) <= 14 else table_flow(b["table"], brand, st, width))
        elif t == "kpis":
            out += [kpi_flow(b["kpis"], brand, st, width), Spacer(1, 10)]
        elif t == "callout":
            out += [callout_flow(b.get("title", ""), b["text"], brand, st, width), Spacer(1, 10)]
        elif t == "chart":
            out.append(KeepTogether(chart_flow(b["chart"], brand, st, width)))
    return out


def heading(num: str, title: str, st: Styles) -> list[Any]:
    return [Paragraph(num, st.num), Paragraph(rich(title, st.f, accents=True), st.h1)]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def _logo(canvas: Any, path: Path | None, x: float, y: float, h: float, *, right: bool = False) -> bool:
    if not path:
        return False
    try:
        img = ImageReader(str(path))
        iw, ih = img.getSize()
        w = h * iw / ih
        canvas.drawImage(img, x - w if right else x, y, width=w, height=h, mask="auto")
        return True
    except Exception:  # noqa: BLE001 — a broken logo never breaks the document
        return False


def render_pdf(spec: dict[str, Any], brand: Brand, meta: DocMeta, out: Path) -> Path:
    f = _register(brand)
    st = Styles(brand, f)
    width = W - ML - MR
    prim, muted, rule = _hex(brand.c("primary")), _hex(brand.c("muted")), _hex(brand.c("rule"))
    short_title = plain(spec["title"])

    def cover(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(prim)
        canvas.rect(0, 0, W, H, stroke=0, fill=1)
        if not _logo(canvas, brand.logo_light, ML, H - 70 - 78, 78):
            canvas.setFillColor(colors.white)
            canvas.setFont(f["semibold"], 13)
            canvas.drawString(ML, H - 110, (brand.company or "ATLAS").upper())
        light = _mix(brand.c("primary"), "#FFFFFF", 0.62)
        y = H * 0.50
        canvas.setStrokeColor(colors.white)
        canvas.setLineWidth(0.8)
        canvas.line(ML, y + 64, ML + 36, y + 64)
        if spec.get("doc_kind"):
            canvas.setFillColor(light)
            canvas.setFont(f["regular"], 8.5)
            canvas.drawString(ML, y + 44, "   ".join(spec["doc_kind"].upper().split()))
        tstyle = ParagraphStyle("ct", fontName=f["light"], fontSize=30, leading=36, textColor=colors.white)
        p = Paragraph(rich(spec["title"], f, accents=True), tstyle)
        _, ph = p.wrap(W - ML - 90, 300)
        p.drawOn(canvas, ML, y + 30 - ph)
        if spec.get("subtitle"):
            sstyle = ParagraphStyle("cs", fontName=f["light"], fontSize=12.5, leading=17, textColor=light)
            sp = Paragraph(rich(spec["subtitle"], f), sstyle)
            _, sh = sp.wrap(W - ML - 120, 200)
            sp.drawOn(canvas, ML, y + 14 - ph - sh)
        canvas.setFillColor(colors.white)
        canvas.setFont(f["regular"], 9)
        canvas.drawString(ML, 96, meta.date_text)
        canvas.setFillColor(light)
        canvas.setFont(f["regular"], 7.5)
        canvas.drawString(ML, 82, " · ".join(x for x in (brand.company, meta.author_line or brand.prepared_by) if x))
        canvas.drawString(ML, 56, brand.footer)
        canvas.restoreState()

    def page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        # running header
        _logo(canvas, brand.logo_dark, W - MR, H - 46, 20, right=True)
        canvas.setFillColor(muted)
        canvas.setFont(f["regular"], 7)
        canvas.drawString(ML, H - 40, short_title[:90])
        canvas.setStrokeColor(rule)
        canvas.setLineWidth(0.4)
        canvas.line(ML, H - 52, W - MR, H - 52)
        # side label
        if brand.label:
            canvas.saveState()
            canvas.translate(30, 90)
            canvas.rotate(90)
            canvas.setFont(f["regular"], 6)
            canvas.setFillColor(muted)
            canvas.drawString(0, 0, "  ".join(f"{brand.label}  |  {meta.date_text[-4:]}".split(" ")))
            canvas.restoreState()
        # footer
        canvas.line(ML, MB - 20, W - MR, MB - 20)
        canvas.setFont(f["regular"], 6.8)
        canvas.drawString(ML, MB - 32, brand.footer)
        canvas.setFillColor(prim)
        canvas.setFont(f["semibold"], 7.5)
        canvas.drawRightString(W - MR, MB - 32, f"{doc.page:02d}")
        canvas.restoreState()

    doc = BaseDocTemplate(str(out), pagesize=letter, leftMargin=ML, rightMargin=MR, topMargin=MT, bottomMargin=MB,
                          title=short_title, author=brand.company or "ATLAS", subject=spec.get("doc_kind", ""),
                          creator="ATLAS")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[Frame(0, 0, W, H, id="c")], onPage=cover),
        PageTemplate(id="body", frames=[Frame(ML, MB, width, H - MT - MB, id="b", leftPadding=0, rightPadding=0,
                                              topPadding=6, bottomPadding=0)], onPage=page),
    ])
    story: list[Any] = [NextPageTemplate("body"), Spacer(1, 1), PageBreak()]

    # executive summary
    story += heading("", "Resumen *ejecutivo*", st)
    for i, para in enumerate(spec["summary"]):
        story.append(Paragraph(rich(para, f), st.lead if i == 0 else st.body))
    if spec.get("highlights"):
        story += [Spacer(1, 8), kpi_flow(spec["highlights"], brand, st, width), Spacer(1, 6)]

    for n, sec in enumerate(spec["sections"], 1):
        story += [CondPageBreak(H * 0.28), Spacer(1, 16)]
        story.append(KeepTogether(heading(f"{n:02d}", sec["title"], st) + blocks_flow(sec["blocks"][:1], brand, st,
                                                                                         width)))
        story += blocks_flow(sec["blocks"][1:], brand, st, width)

    # notes, sources, verification
    story += [PageBreak(), *heading("", "Notas y *fuentes*", st)]
    if spec.get("sources"):
        story += [Paragraph("Fuentes", st.caption), bullets_flow(spec["sources"], brand, st)]
    for title, lines in meta.notes.items():
        if lines:
            story += [Paragraph(rich(title, f), st.caption), bullets_flow(lines, brand, st)]
    story += [Spacer(1, 10), Rule(width, rule),
              Paragraph(rich(f"{brand.prepared_by} · {meta.date_text}. Las cifras provienen de los reportes de los "
                             "agentes y de la evidencia registrada por el sistema en la misión.", f), st.source)]
    doc.build(story)
    return out
