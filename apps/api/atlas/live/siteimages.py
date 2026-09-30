"""Images from public websites (tool `collect_site_images`, docs/PUBLISHING.md § Photos).

Any public http(s) site can be collected from. Where an image comes from is recorded, never refused:

    ATLAS_OWN_SITES   the company's own domains, ',' or ';' separated (e.g. pagadesarrollos.com). Their images are
                      the company's material and go to imagenes/. Images of any other site go to
                      imagenes/<domain>/ and are third-party material: usable with the source credited.

Every saved image is listed in imagenes/fuentes.csv (file, origin, image URL, page) so each one can be credited.
Private addresses (localhost, the LAN, the tailnet) are never opened: the server must not browse itself.

The page is opened in a real headless browser (so sites built with Wix, Webflow, WordPress page builders or
JavaScript galleries work), scrolled to load lazy images, and every large image is collected: <img> (the largest
srcset candidate), <picture> sources, CSS background images and og:image. Images smaller than `min_px` on their
longest side (icons, logos, thumbnails) are skipped. Files go to the mission outputs under imagenes/, where
write_deliverable picks them up by path. The page's links to other pages of the same site are returned too, so
the agent can walk to each project page.

Read-only: nothing is submitted, no login, a fresh browser each call (never the agents' signed-in profiles).
"""

from __future__ import annotations

import csv
import io
import ipaddress
import os
import re
import socket
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_IMAGES = 24
MIN_PX = 600
MAX_BYTES = 15 * 1024 * 1024
MAX_LINKS = 60
PAGE_TIMEOUT_MS = 45_000


class SiteImagesError(Exception):
    """A refusal or failure the agent can read."""


def own_sites() -> list[str]:
    raw = os.getenv("ATLAS_OWN_SITES", "")
    out = []
    for part in re.split(r"[;,\s]+", raw):
        host = part.strip().lower()
        host = urlparse(host).hostname or host if "://" in host else host.split("/")[0]
        host = host.removeprefix("www.")
        if host and host not in out:
            out.append(host)
    return out


def site_allowed(url: str, sites: list[str] | None = None) -> bool:
    sites = own_sites() if sites is None else sites
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return parsed.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in sites)


def _private_ok() -> bool:
    """Tests serve a site on 127.0.0.1; production never opens private addresses."""
    return os.getenv("ATLAS_SITE_IMAGES_ALLOW_PRIVATE", "") == "1"


_HOST_CACHE: dict[str, bool] = {}


def public_host(host: str) -> bool:
    """True when every address of `host` is public (not loopback, LAN, link-local, CGNAT/tailnet or reserved)."""
    host = (host or "").lower().strip("[]")
    if not host:
        return False
    if host in _HOST_CACHE:
        return _HOST_CACHE[host]
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        addrs = set()
    ok = bool(addrs)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip in ipaddress.ip_network("100.64.0.0/10"):
            ok = False
    _HOST_CACHE[host] = ok
    return ok


def url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return _private_ok() or public_host(parsed.hostname)


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _slug(text: str, n: int = 50) -> str:
    norm = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in norm if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n].strip("-") or "imagen"


# Everything the page shows as an image, with the best URL of each (the largest srcset candidate).
_COLLECT_JS = r"""
() => {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const best = (srcset) => {
    let top = null, w = -1;
    for (const part of (srcset || '').split(',')) {
      const [u, d] = part.trim().split(/\s+/);
      if (!u) continue;
      const v = parseFloat(d) || 1;
      if (v > w) { w = v; top = u; }
    }
    return top;
  };
  const out = [];
  const add = (u, alt, w, h, kind) => { const a = abs(u); if (a && !a.startsWith('data:')) out.push({url: a, alt: alt || '', w: w || 0, h: h || 0, kind}); };
  for (const img of document.querySelectorAll('img')) {
    const u = best(img.getAttribute('srcset') || img.getAttribute('data-srcset')) || img.currentSrc || img.src
      || img.getAttribute('data-src') || img.getAttribute('data-lazy-src');
    if (u) add(u, img.alt || img.title, img.naturalWidth, img.naturalHeight, 'img');
  }
  for (const s of document.querySelectorAll('picture source')) {
    const u = best(s.getAttribute('srcset')); if (u) add(u, '', 0, 0, 'picture');
  }
  for (const el of document.querySelectorAll('*')) {
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg.startsWith('url(')) for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) add(m[1], el.getAttribute('aria-label') || '', el.clientWidth, el.clientHeight, 'background');
  }
  const og = document.querySelector('meta[property="og:image"]'); if (og) add(og.content, 'og:image', 0, 0, 'og');
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const u = abs(a.getAttribute('href'));
    if (u && new URL(u).hostname === location.hostname && !u.includes('#')) links.push({url: u, text: (a.innerText || a.title || '').trim().slice(0, 80)});
  }
  return {title: document.title, images: out, links};
}
"""


@dataclass
class Collected:
    page: str
    title: str = ""
    saved: list[dict[str, Any]] = field(default_factory=list)  # {path, name, width, height, alt, source}
    skipped: int = 0
    own: bool = False
    domain: str = ""
    links: list[dict[str, str]] = field(default_factory=list)


def _launch(pw: Any) -> Any:
    from . import browser as B

    exe = os.getenv("ATLAS_BROWSER_EXECUTABLE", "").strip()
    tries: list[dict[str, Any]] = [{"executable_path": os.path.expanduser(exe)}] if exe else []
    if B.channel() != "chromium":
        tries.append({"channel": B.channel()})
    tries.append({})
    last: Exception | None = None
    for opts in tries:
        try:
            return pw.chromium.launch(headless=True, **opts)
        except Exception as exc:  # noqa: BLE001 — try the next browser
            last = exc
    raise SiteImagesError(f"could not start a browser: {str(last).splitlines()[0] if last else '?'}")


def collect(url: str, dest: Path, *, max_images: int = MAX_IMAGES, min_px: int = MIN_PX,
            sites: list[str] | None = None) -> Collected:
    """Open `url` (any public site) and save its large images. Synchronous.

    The company's own sites (ATLAS_OWN_SITES) save into `dest`; any other site into `dest/<domain>/`.
    """
    sites = own_sites() if sites is None else sites
    if not url_allowed(url):
        raise SiteImagesError(f"{url} can't be opened: only public http(s) websites (never private or local addresses)")
    own = site_allowed(url, sites)
    domain = domain_of(url)
    folder = dest if own else dest / _slug(domain, 60)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SiteImagesError("Playwright is not installed (uv sync)") from exc
    from PIL import Image

    from . import browser as B

    B._proactor_on_windows()
    result = Collected(page=url, own=own, domain=domain)
    with sync_playwright() as pw:
        br = _launch(pw)
        try:
            ctx = br.new_context(viewport={"width": 1600, "height": 1000}, accept_downloads=False)
            # the page and its redirects may only reach public addresses
            ctx.route("**/*", lambda route: route.continue_() if url_allowed(route.request.url) else route.abort())
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — slow trackers never finish: work with what loaded
                page.wait_for_timeout(3000)
            for _ in range(12):  # lazy-loaded galleries appear on scroll
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(350)
            page.wait_for_timeout(1200)
            data = page.evaluate(_COLLECT_JS)
            result.title = str(data.get("title") or "")
            seen_links: set[str] = set()
            for ln in data.get("links") or []:
                u = ln.get("url") or ""
                if u and u.rstrip("/") != url.rstrip("/") and u not in seen_links and domain_of(u) == domain:
                    seen_links.add(u)
                    result.links.append({"url": u, "text": ln.get("text") or ""})
            result.links = result.links[:MAX_LINKS]
            folder.mkdir(parents=True, exist_ok=True)
            seen: set[str] = set()
            digests: set[int] = set()
            for cand in data.get("images") or []:
                src = str(cand.get("url") or "")
                if not src or src in seen or re.search(r"\.svg(\?|$)", src, re.IGNORECASE) or not url_allowed(src):
                    continue
                seen.add(src)
                if len(result.saved) >= max_images:
                    result.skipped += 1
                    continue
                known = max(int(cand.get("w") or 0), int(cand.get("h") or 0))
                if cand.get("kind") == "img" and 0 < known < min_px // 3:
                    result.skipped += 1  # an icon or thumbnail as displayed; its srcset gave nothing bigger
                    continue
                try:
                    resp = ctx.request.get(src, headers={"Referer": url}, timeout=30_000)
                    body = resp.body() if resp.ok else b""
                except Exception:  # noqa: BLE001 — one broken image never stops the rest
                    body = b""
                if not body or len(body) > MAX_BYTES:
                    result.skipped += 1
                    continue
                try:
                    with Image.open(io.BytesIO(body)) as im:
                        w, h = im.size
                        fmt = (im.format or "").lower()
                        if max(w, h) < min_px:
                            result.skipped += 1
                            continue
                        if fmt not in ("jpeg", "png", "webp", "gif"):
                            buf = io.BytesIO()
                            im.convert("RGB").save(buf, "JPEG", quality=90)
                            body, fmt = buf.getvalue(), "jpeg"
                except Exception:  # noqa: BLE001 — not an image (or a format Pillow can't read)
                    result.skipped += 1
                    continue
                digest = hash(body)
                if digest in digests:  # the same picture under two URLs
                    continue
                digests.add(digest)
                ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}[fmt]
                stem = _slug(str(cand.get("alt") or "") or Path(urlparse(src).path).stem)
                name, n = f"{stem}{ext}", 2
                while (folder / name).exists():
                    name = f"{stem}-{n}{ext}"
                    n += 1
                (folder / name).write_bytes(body)
                rel = name if own else f"{folder.name}/{name}"
                result.saved.append({"path": str(folder / name), "name": rel, "width": w, "height": h,
                                     "alt": str(cand.get("alt") or ""), "source": src})
        finally:
            br.close()
    if result.saved:
        _record_sources(dest, result)
    return result


def _record_sources(dest: Path, got: Collected) -> None:
    """imagenes/fuentes.csv: one row per saved image, so every picture can be credited."""
    index = dest / "fuentes.csv"
    new = not index.exists()
    with index.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["archivo", "origen", "dominio", "imagen", "pagina"])
        for i in got.saved:
            w.writerow([f"imagenes/{i['name']}", "propia" if got.own else "terceros", got.domain, i["source"],
                        got.page])


def page_url(raw: str) -> str:
    """A bare domain or path → a URL."""
    raw = (raw or "").strip()
    return "https://" + raw.lstrip("/") if raw and "://" not in raw else raw
