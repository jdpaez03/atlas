"""Sales brochures (publish/brochure.py) and IRIS's design tools (live/designtools.py)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from atlas.live import designtools
from atlas.live.files import FileSandbox, FileTools
from atlas.publish import brochure as B

MID = "msn_brochure01"


@pytest.fixture
def kit(tmp_path: Path, monkeypatch) -> Path:
    local = tmp_path / "local"
    monkeypatch.setenv("ATLAS_LOCAL_DIR", str(local))
    monkeypatch.setenv("ATLAS_FILE_ROOTS_CORPORATE", str(tmp_path / "src"))
    k = local / "brand" / "aqua"
    k.mkdir(parents=True)
    logo = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    logo.paste((4, 52, 71, 255), (20, 20, 280, 100))
    logo.save(k / "logo.png")
    (k / "brand.yaml").write_text(
        "name: Torre Aqua\nfooter: Torre Aqua\nshape: drop\n"
        "colors: {paper: '#FFFFF8', deep: '#043447', primary: '#2B5C63', accent: '#69ACB7', tint: nope}\n"
        "fonts: {display: {light: fonts/missing.ttf}}\nlogo_dark: logo.png\nisotype: logo.png\n"
        "logo_light: ../../escape.png\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (1600, 1000), (40, 90, 160)).save(src / "render.jpg")
    Image.new("RGB", (900, 1400), (230, 230, 230)).save(src / "plano.png")
    return src


def test_kit_and_spec(kit):
    k = B.load_kit("aqua")
    assert k.configured and k.shape == "drop" and k.c("deep") == "#043447" and k.c("tint") == B.DEFAULT_COLORS["tint"]
    assert k.logo_light == k.logo_dark  # a path outside the kit is ignored; the other logo is used
    assert k.fonts["display"] == {} and "aqua" in B.kit_slugs()
    spec, errors = B.normalize({"kit": "aqua", "pages": [
        {"type": "hero", "image": "render.jpg", "title": "La forma|más pura de vivir."},
        {"type": "hero", "title": "sin imagen"},
        {"type": "nope"},
        {"type": "table", "title": "Tipologías", "table": {"columns": ["Tipo", "m²"], "rows": [["A", 157.09]]}}]})
    assert [p["type"] for p in spec["pages"]] == ["hero", "table"] and len(errors) == 2
    assert B.image_refs(spec) == ["render.jpg"]
    assert B._title("La forma|más pura") == '<h2 class="h2">La forma<br><em>más pura</em></h2>'


def test_render_prints_every_page_shrinks_text_and_reports_overflow(kit):
    spec, _ = B.normalize({"kit": "aqua", "pages": [
        {"type": "cover", "title": "Torre Aqua", "subtitle": "Monterrey"},
        {"type": "split", "image": "render.jpg", "title": "Una torre|que nace del bosque.", "text": "Diseñada. " * 30,
         "stats": [{"value": "50", "label": "niveles"}]},
        {"type": "columns", "title": "Un resort|para toda la familia.", "columns": [
            {"heading": h, "items": ["Una amenidad descrita con demasiadas palabras para caber en la columna " * 2] * 10}
            for h in ("Deportivas", "Sociales", "Servicios")]},
        {"type": "plan", "title": "Planta baja", "items": [{"title": "Lobby", "text": "doble altura " * 30}] * 8,
         "image": "plano.png"}]})
    imgs = {"render.jpg": kit / "render.jpg", "plano.png": kit / "plano.png"}
    r = B.render(spec, B.load_kit("aqua"), imgs)
    assert len(PdfReader(io.BytesIO(r.pdf)).pages) == 4 and len(r.previews) == 4
    # page 3 (30 long items) fits by shrinking its text; page 4 (8 paragraphs in a narrow column) can't
    assert {w[:6] for w in r.warnings} == {"page 4"}
    html = B.build_html(spec, B.load_kit("aqua"), imgs, kit)
    assert "rgba(" not in html and "opacity" not in html and "mix-blend" not in html  # no transparency at all
    assert B.contact_sheet(r.previews)[:2] == b"\xff\xd8"


def test_design_tools_extract_see_crop_and_write(kit, monkeypatch):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    cmyk = Image.new("CMYK", (800, 600), (0, 255, 255, 0))  # pure red in CMYK
    cmyk.save(kit / "rojo.jpg", "JPEG")
    pdf = kit / "esquematico.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(800, 600))
    c.drawImage(ImageReader(str(kit / "rojo.jpg")), 0, 0, 800, 600)
    c.drawImage(ImageReader(str(kit / "render.jpg")), 0, 0, 160, 100)
    c.showPage()
    c.save()
    files = FileTools(FileSandbox.for_mission("corporate", MID))

    res = files.run("pdf_images", {"path": str(pdf), "min_px": 700})
    assert res.ok and "Extracted 2 image(s)" in res.text and res.images
    out = files.sb.outputs / "extraidas" / "esquematico"
    red = Image.open(next(out.glob("p01_*.jpg"))).convert("RGB").getpixel((400, 300))
    assert red[0] > 200 and red[1] < 60  # real colors, not the inverted CMYK of a raw dump

    res = files.run("page_image", {"path": str(pdf), "page": 1, "dpi": 72})
    assert res.ok and "paginas/esquematico_p01.png" in res.text
    res = files.run("crop_image", {"path": "paginas/esquematico_p01.png",
                                   "box": [0, 0, 0.5, 0.5], "name": "logo", "transparent": True})
    assert res.ok and (files.sb.outputs / "recortes" / "logo.png").is_file()
    assert not files.run("crop_image", {"path": str(pdf), "box": [0.5, 0, 0.2, 1], "name": "x"}).ok

    assert designtools.tools([]) == [] and [t["name"] for t in designtools.tools(["sales_brochure"])] == list(
        designtools.NAMES)
    res = files.run("write_brochure", {"filename": "Aqua_v1", "brochure": {"kit": "aqua", "pages": [
        {"type": "hero", "image": str(kit / "render.jpg"), "title": "La forma|más pura de vivir."},
        {"type": "contact", "lines": ["[Nombre del asesor]"]}]}})
    assert res.ok and "Aqua_v1.pdf" in res.text and res.attachment and res.images
    assert len(PdfReader(str(files.sb.outputs / "Aqua_v1.pdf")).pages) == 2
    leak = files.run("write_brochure", {"filename": "x", "brochure": {"kit": "aqua", "pages": [
        {"type": "statement", "title": "Dato HUECO en esta corrida"}]}})
    assert not leak.ok and "working notes" in leak.text


def test_knock_out_background():
    im = Image.new("RGB", (100, 60), (245, 245, 240))
    im.paste((4, 52, 71), (30, 20, 70, 40))
    cut = designtools.knock_out_background(im)
    assert cut.getpixel((5, 5))[3] == 0 and cut.getpixel((50, 30))[3] == 255
