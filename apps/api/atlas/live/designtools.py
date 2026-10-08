"""Sales-material tools (docs/BROCHURES.md): only for agents with the `sales_brochure` capability (IRIS).

    pdf_images(path)              the photos/renders/plans embedded in a PDF → extraidas/<pdf>/ + a numbered sheet
    page_image(path, page)        one PDF page as an image → paginas/ (with a 10% grid on the view, for cropping)
    crop_image(path, box)         a region of an image → recortes/ (optionally the background turned transparent)
    write_brochure(filename, …)   the sales brochure (PDF 16:9) in the project's brand kit (publish/brochure.py)

Everything is written to the mission outputs; the agent SEES each result (contact sheets, crops, page previews).
Embedded images are decoded by PDFium, so CMYK/Adobe JPEGs come out with their real colors (no inversion).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import paths

if TYPE_CHECKING:
    from .files import FileResult, FileTools

CAPABILITY = "sales_brochure"
SOURCE_MAX_BYTES = 150 * 1024 * 1024  # an architect's schematic set is easily 60-120 MB
MAX_EXTRACT = 80
VIEW_SIDE = 1568

_T = {"type": "string"}

PDF_IMAGES_TOOL: dict[str, Any] = {
    "name": "pdf_images",
    "description": (
        "Extract the images embedded in a PDF (renders, photos, plans, logos) as files, with their real colors, "
        "and SEE them in one numbered contact sheet. Use it on the architect's schematic, the brandbook, earlier "
        "presentations. Small images (icons) are skipped with min_px. Files land in extraidas/<pdf name>/ and are "
        "usable in write_brochure. A page with text over the photo isn't a clean render: prefer the embedded image."
    ),
    "input_schema": {"type": "object", "properties": {
        "path": {"type": "string", "description": "a PDF you can read (onedrive:/…, attachments, outputs)"},
        "pages": {"type": "string", "description": "e.g. '1-5,40-48' (default all)"},
        "min_px": {"type": "integer", "minimum": 100, "description": "smallest longest side kept (default 600)"},
    }, "required": ["path"]},
}

PAGE_IMAGE_TOOL: dict[str, Any] = {
    "name": "page_image",
    "description": (
        "Render one page of a PDF as an image (paginas/<pdf>_p<N>.png) and SEE it with a 10% grid drawn over the "
        "view, to read coordinates for crop_image (plans, logos, a table of areas). The saved file has no grid."
    ),
    "input_schema": {"type": "object", "properties": {
        "path": _T, "page": {"type": "integer", "minimum": 1},
        "dpi": {"type": "integer", "minimum": 72, "maximum": 300, "description": "default 200"},
    }, "required": ["path", "page"]},
}

CROP_IMAGE_TOOL: dict[str, Any] = {
    "name": "crop_image",
    "description": (
        "Cut a region of an image into recortes/<name>.png and SEE it. box = [left, top, right, bottom] as "
        "fractions of the image (0-1), read off page_image's grid. transparent: the flat background (taken from "
        "the corners) becomes transparent — for logos to place on the brochure's own colors."
    ),
    "input_schema": {"type": "object", "properties": {
        "path": _T, "name": _T,
        "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "transparent": {"type": "boolean"},
    }, "required": ["path", "box", "name"]},
}


def tools(capabilities: list[str]) -> list[dict[str, Any]]:
    from ..publish.brochure import BROCHURE_SCHEMA

    if CAPABILITY not in (capabilities or []):
        return []
    write = {
        "name": "write_brochure",
        "description": (
            "Write the SALES BROCHURE as a 16:9 PDF in the project's brand kit (colors, fonts, logo, the shape that "
            "cuts feature images). You give each page's type and content; the system lays it out, shrinks text "
            "that doesn't fit and reports what still overflows, and you SEE every page in a contact sheet: check it "
            "and write again with a new file name until nothing is off. Project kits on this machine: "
            + (", ".join(_kits()) or "none yet (ask the human for the project's brandbook)") + ". Each kit folder "
            "(brand/<slug>/) may hold brief.md (approved facts and copy: read it FIRST, its figures are confirmed) "
            "and assets/ (finished images and maps: use them before extracting from sources). Developers' track "
            "records (logos + trayectoria.md): " + (", ".join(_other_brand_folders()) or "none") + "."
        ),
        "input_schema": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "e.g. 'Torre_Acqua_Brochure_v1'"},
            "brochure": BROCHURE_SCHEMA,
        }, "required": ["filename", "brochure"]},
    }
    return [PDF_IMAGES_TOOL, PAGE_IMAGE_TOOL, CROP_IMAGE_TOOL, write]


NAMES = ("pdf_images", "page_image", "crop_image", "write_brochure")


def _other_brand_folders() -> list[str]:
    """brand/<x>/ folders that aren't project kits but hold a trayectoria.md (developers' track records)."""
    from ..publish.brochure import kits_dir

    root = kits_dir()
    return sorted(d.name for d in root.iterdir() if (d / "trayectoria.md").is_file()) if root.is_dir() else []


def _kits() -> list[str]:
    from ..publish.brochure import kit_slugs

    return kit_slugs()


# ----------------------------------------------------------------------------------------------------------------


def _jpeg(im: Any, side: int = VIEW_SIDE) -> bytes:
    im = im.convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _pages(spec: str | None, n: int) -> list[int]:
    if not spec:
        return list(range(n))
    out: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out += [p - 1 for p in range(min(a, b), max(a, b) + 1) if 1 <= p <= n]
    return list(dict.fromkeys(out))


def _stem(path: Path) -> str:
    return paths.safe_name(path.stem)[:60] or "pdf"


def _unique(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    target, n = folder / name, 2
    while target.exists():
        target = folder / f"{stem} ({n}){ext}"
        n += 1
    return target


def numbered_sheet(items: list[tuple[str, Any]], cols: int = 5, cell: int = 300) -> bytes:
    """A contact sheet: each image with its label underneath (what the agent cites to pick files)."""
    from PIL import Image, ImageDraw

    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 22)), "#FFFFFF")
    d = ImageDraw.Draw(sheet)
    for i, (label, im) in enumerate(items):
        th = im.convert("RGB")
        th.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * (cell + 22)
        sheet.paste(th, (x + (cell - th.width) // 2, y + (cell - th.height) // 2))
        d.text((x + 4, y + cell + 4), label[:44], fill="#222222")
    return _jpeg(sheet, 1600)


class DesignTools:
    """Bound to the agent's FileTools: same sandbox (what it may read), same mission outputs."""

    def __init__(self, files: FileTools):
        self.f = files

    @property
    def out(self) -> Path:
        return self.f.sb.outputs

    def _local(self, ref: str) -> Path:
        from .files import FileAccessError
        from .graphfiles import GraphFilesError

        try:
            remote = self.f._remote_target(ref)
            if remote is not None:
                item = self.f._remote_item(*remote)
                if item.is_dir:
                    raise FileAccessError(f"{ref} is a folder")
                return self.f.graph.download(item, max_bytes=SOURCE_MAX_BYTES)
            p = self.f.sb.resolve(ref)
        except GraphFilesError as exc:
            raise FileAccessError(str(exc)) from exc
        if p.is_dir():
            raise FileAccessError(f"{ref} is a folder")
        if p.stat().st_size > SOURCE_MAX_BYTES:
            raise FileAccessError(f"{ref} is larger than {SOURCE_MAX_BYTES // (1024 * 1024)} MB")
        return p

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.out).as_posix()
        except ValueError:
            return str(p)

    # -- tools ---------------------------------------------------------------------------------------------------

    def pdf_images(self, path: str = "", pages: str | None = None, min_px: Any = None) -> FileResult:
        import pypdfium2 as pdfium
        from pypdfium2 import raw

        from .files import FileAccessError, FileResult

        src = self._local(path)
        if src.suffix.lower() != ".pdf":
            raise FileAccessError("pdf_images reads PDFs (for an image file use read_file / crop_image)")
        try:
            min_side = max(100, int(min_px or 600))
        except (TypeError, ValueError):
            min_side = 600
        folder = self.out / "extraidas" / _stem(src)
        doc = pdfium.PdfDocument(str(src))
        saved: list[tuple[str, Any, tuple[int, int]]] = []
        seen: set[str] = set()
        skipped = 0
        try:
            for pi in _pages(pages, len(doc)):
                page = doc[pi]
                for oi, obj in enumerate(page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3)):
                    if len(saved) >= MAX_EXTRACT:
                        break
                    try:
                        im = obj.get_bitmap(render=False).to_pil()
                    except Exception:  # noqa: BLE001 — an undecodable image is skipped
                        skipped += 1
                        continue
                    if max(im.size) < min_side:
                        skipped += 1
                        continue
                    key = im.convert("L").resize((16, 16)).tobytes().hex()
                    if key in seen:  # the same image repeated on several pages
                        continue
                    seen.add(key)
                    name = f"p{pi + 1:02d}_{oi + 1:02d}"
                    rgb = im.convert("RGB")
                    target = _unique(folder, name + ".jpg")
                    rgb.save(target, "JPEG", quality=92)
                    saved.append((self._rel(target), rgb, im.size))
        finally:
            doc.close()
        if not saved:
            return FileResult(f"No images of at least {min_side}px in {path} ({skipped} smaller or unreadable). "
                              "Its drawings may be vector: use page_image and crop_image.", True, "file_read",
                              path, "no images")
        lines = [(f"Extracted {len(saved)} image(s) from {path} into {self._rel(folder)}/ (numbered as in the "
                  f"sheet; {skipped} small or unreadable skipped). The contact sheet is attached: look at it.")]
        lines += [f"{i + 1}. {rel}  {w}×{h}" for i, (rel, _, (w, h)) in enumerate(saved)]
        res = FileResult("\n".join(lines), True, "file_read", path, f"{len(saved)} images extracted")
        res.images.append(("image/jpeg", numbered_sheet([(f"{i + 1} · {Path(r).name}", im)
                                                          for i, (r, im, _) in enumerate(saved)])))
        return res

    def page_image(self, path: str = "", page: Any = 1, dpi: Any = None) -> FileResult:
        import pypdfium2 as pdfium
        from PIL import ImageDraw

        from .files import FileAccessError, FileResult

        src = self._local(path)
        doc = pdfium.PdfDocument(str(src))
        try:
            n = int(page or 1)
            if not 1 <= n <= len(doc):
                raise FileAccessError(f"page {n} doesn't exist ({len(doc)} pages)")
            scale = max(72, min(300, int(dpi or 200))) / 72
            im = doc[n - 1].render(scale=scale).to_pil().convert("RGB")
        finally:
            doc.close()
        target = _unique(self.out / "paginas", f"{_stem(src)}_p{n:02d}.png")
        im.save(target, "PNG", optimize=True)
        view = im.copy()
        view.thumbnail((VIEW_SIDE, VIEW_SIDE))
        d = ImageDraw.Draw(view)
        w, h = view.size
        for k in range(1, 10):
            x, y = round(w * k / 10), round(h * k / 10)
            d.line([(x, 0), (x, h)], fill="#FF2D55", width=1)
            d.line([(0, y), (w, y)], fill="#FF2D55", width=1)
            d.text((x + 2, 2), f".{k}", fill="#FF2D55")
            d.text((2, y + 2), f".{k}", fill="#FF2D55")
        res = FileResult(f"Page {n} of {path} saved as {self._rel(target)} ({im.width}×{im.height}). Attached with a "
                         "10% grid (red, labels .1–.9) to read crop boxes; the file has no grid.", True,
                         "file_read", path, f"page {n} rendered")
        res.images.append(("image/jpeg", _jpeg(view)))
        return res

    def crop_image(self, path: str = "", box: Any = None, name: str = "", transparent: Any = False) -> FileResult:
        from PIL import Image

        from .files import FileAccessError, FileResult

        src = self._local(path)
        try:
            l, t, r, b = (float(x) for x in (box or []))
        except (TypeError, ValueError) as exc:
            raise FileAccessError("box must be [left, top, right, bottom] as fractions 0-1") from exc
        if not (0 <= l < r <= 1 and 0 <= t < b <= 1):
            raise FileAccessError("box must be [left, top, right, bottom] with 0 ≤ left < right ≤ 1, same for top/bottom")
        with Image.open(src) as im0:
            im = im0.convert("RGBA")
        W, H = im.size
        cut = im.crop((round(l * W), round(t * H), round(r * W), round(b * H)))
        if transparent is True or str(transparent).lower() == "true":
            cut = knock_out_background(cut)
        target = _unique(self.out / "recortes", paths.safe_name(name or "recorte")[:60] + ".png")
        cut.save(target, "PNG", optimize=True)
        res = FileResult(f"Saved {self._rel(target)} ({cut.width}×{cut.height}). Attached: check it before using it.",
                         True, "file_written", str(target), f"crop of {Path(path).name}")
        view = Image.new("RGB", cut.size, "#B9C2C8")  # a grey backdrop shows what became transparent
        view.paste(cut, mask=cut.getchannel("A"))
        res.images.append(("image/jpeg", _jpeg(view)))
        return res

    def write_brochure(self, filename: str = "", brochure: Any = None) -> FileResult:
        from ..core.models import Attachment
        from ..publish import brochure as B
        from .files import FileAccessError, FileResult, download_url

        if isinstance(brochure, str):
            import json

            try:
                brochure = json.loads(brochure)
            except json.JSONDecodeError as exc:
                raise FileAccessError(f"`brochure` is not valid JSON: {exc}") from exc
        spec, errors = B.normalize(brochure)
        if not spec["pages"]:
            raise FileAccessError("; ".join(errors) or "no pages")
        from ..publish.spec import internal_terms

        leaks = internal_terms({"slides": [{"title": p["title"], "text": p["text"], "items": [i["title"] + " "
                                            + i["text"] for i in p["items"]]} for p in spec["pages"]]})
        if leaks:
            raise FileAccessError("The brochure shows working notes or tool names: " + "; ".join(leaks)
                                  + ". A buyer reads this: rewrite or drop those passages.")
        kit = B.load_kit(spec["kit"])
        images: dict[str, Path] = {}
        missing: list[str] = []
        for ref in B.image_refs(spec):
            p, why = self.f._image_file(ref)
            if p is None:
                missing.append(f"{ref} ({why})")
            else:
                images[ref] = p
        rendered = B.render(spec, kit, images)
        name = paths.safe_name(re.sub(r"\.pdf$", "", str(filename or "brochure"), flags=re.IGNORECASE))[:80] + ".pdf"
        target = _unique(self.out, name)
        target.write_bytes(rendered.pdf)
        att = Attachment(name=target.name, kind="file", size_bytes=len(rendered.pdf),
                         download_url=download_url(self.f.sb.mission_id, target.name))
        notes = errors + [f"image not readable: {m}" for m in missing] + rendered.warnings
        text = (f"Wrote {target.name} ({len(spec['pages'])} pages, {len(rendered.pdf) // 1024:,} KB) in kit "
                f"'{kit.slug}'. Download link for the human: {att.download_url}\nThe contact sheet of every page is "
                "attached: look at each page (text over busy areas, a photo repeated, a crop that cuts the subject, "
                "an empty-looking page) and write a new version if anything is off.")
        if notes:
            text += "\nFix these and write again:\n- " + "\n- ".join(notes[:15])
        res = FileResult(text, True, "file_written", str(target), f"brochure, {len(spec['pages'])} pages",
                         attachment=att)
        res.images.append(("image/jpeg", B.contact_sheet(rendered.previews)))
        return res


def knock_out_background(im: Any, tolerance: int = 38) -> Any:
    """Flat background (median of the 4 corners) → transparent, with a soft edge (a logo cut from a page)."""
    from PIL import Image, ImageChops

    rgba = im.convert("RGBA")
    rgb = rgba.convert("RGB")
    w, h = rgb.size
    px = [rgb.getpixel(p) for p in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
    bg = tuple(sorted(c[i] for c in px)[1] for i in range(3))  # a median-ish corner color
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    r, g, b = diff.split()
    dist = ImageChops.lighter(ImageChops.lighter(r, g), b)  # the largest channel difference
    lo, hi = tolerance * 0.35, tolerance
    alpha = dist.point(lambda v: 0 if v <= lo else 255 if v >= hi else int((v - lo) / (hi - lo) * 255))
    alpha = ImageChops.multiply(alpha, rgba.getchannel("A"))
    rgba.putalpha(alpha)
    return rgba
