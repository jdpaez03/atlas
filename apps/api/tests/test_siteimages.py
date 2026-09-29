"""collect_site_images: renders from the company's own website, into the mission outputs (real headless browser)."""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from atlas.core import paths
from atlas.live import siteimages as si
from atlas.live.files import FileSandbox, FileTools

MID = "msn_site0001"


def _png(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _jpg(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


FILES = {
    "/img/fachada-800.jpg": ("image/jpeg", _jpg(800, 450, (10, 60, 120))),
    "/img/fachada-1600.jpg": ("image/jpeg", _jpg(1600, 900, (10, 60, 120))),
    "/img/lobby.png": ("image/png", _png(1200, 800, (200, 150, 90))),
    "/img/roof.jpg": ("image/jpeg", _jpg(1400, 900, (30, 160, 90))),
    "/img/icon.png": ("image/png", _png(64, 64, (0, 0, 0))),
    "/img/logo.svg": ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
}
HOME = """<html><head><title>PAGA · Proyectos</title><meta property="og:image" content="/img/roof.jpg"></head><body>
<a href="/fiori">Torre Fiori</a> <a href="https://elsewhere.example/x">fuera</a>
<img src="/img/fachada-800.jpg" srcset="/img/fachada-800.jpg 800w, /img/fachada-1600.jpg 1600w" alt="Fachada Torre Fiori">
<img data-src="/img/lobby.png" alt="Lobby" class="lazy">
<img src="/img/icon.png" width="32"> <img src="/img/logo.svg">
<div style="width:900px;height:500px;background-image:url('/img/roof.jpg')" aria-label="Roof garden"></div>
<script>document.querySelectorAll('img.lazy').forEach(i => i.src = i.dataset.src)</script>
</body></html>"""


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/fiori"):
            body, ctype = HOME.encode(), "text/html; charset=utf-8"
        elif path in FILES:
            ctype, body = FILES[path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def browser_env(monkeypatch):
    if Path("/opt/pw-browsers/chromium").exists():
        monkeypatch.setenv("ATLAS_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        pytest.skip("Playwright not installed")


def test_own_sites_parsing(monkeypatch):
    monkeypatch.setenv("ATLAS_OWN_SITES", "https://www.PagaDesarrollos.com/proyectos; torrefiori.mx ,")
    assert si.own_sites() == ["pagadesarrollos.com", "torrefiori.mx"]
    assert si.site_allowed("https://pagadesarrollos.com/fiori") and si.site_allowed("https://img.torrefiori.mx/a")
    assert not si.site_allowed("https://pagadesarrollos.com.evil.io/") and not si.site_allowed("ftp://torrefiori.mx")
    assert si.page_url("pagadesarrollos.com/fiori") == "https://pagadesarrollos.com/fiori"


def test_collects_large_images_and_uses_them_in_a_deck(site, browser_env, monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_OWN_SITES", "127.0.0.1")
    monkeypatch.setenv("ATLAS_FILE_ROOTS_CORPORATE", str(tmp_path))
    tools = FileTools(FileSandbox.for_mission("corporate", MID))
    res = tools.run("collect_site_images", {"url": site + "/"})
    assert res.ok and res.kind == "web_fetch", res.text
    out = paths.outputs_dir("corporate", MID) / "imagenes"
    names = sorted(p.name for p in out.iterdir())
    assert names == ["fachada-torre-fiori.jpg", "lobby.png", "roof-garden.jpg"], names  # icon, svg, dup og skipped
    with Image.open(out / "fachada-torre-fiori.jpg") as im:
        assert im.size == (1600, 900)  # the largest srcset candidate
    assert "imagenes/lobby.png  1200×800" in res.text and f"{site}/fiori" in res.text and "elsewhere" not in res.text
    deck = tools.run("write_deliverable", {"filename": "Fiori", "format": "pptx", "document": {
        "title": "Torre Fiori", "cover_image": "imagenes/fachada-torre-fiori.jpg",
        "slides": [{"type": "image", "image": "imagenes/lobby.png", "title": "Lobby"}]}})
    assert deck.ok and "images: fachada-torre-fiori.jpg, lobby.png" in deck.detail, deck.text

    refused = tools.run("collect_site_images", {"url": "https://example.com/"})
    assert not refused.ok and "not one of the company's own sites" in refused.text
    monkeypatch.delenv("ATLAS_OWN_SITES")
    assert "ATLAS_OWN_SITES" in tools.run("collect_site_images", {"url": site}).text
