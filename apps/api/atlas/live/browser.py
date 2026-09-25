"""Opt-in dedicated browser for agents (docs/BROWSER.md).

An agent whose YAML has a `browser:` block gets browser_* tools that drive ITS OWN persistent browser
profile (<ATLAS_LOCAL_DIR>/browser/<profile>/) — never the user's everyday Chrome, tabs or logins.
The human signs into the site once in that profile:

    uv run atlas-browser login <agent-id>        (or: uv run python -m atlas.live.browser login <agent-id>)

and the agent reuses the session: it opens pages, reads them, clicks, fills filters, extracts tables and
downloads exports. Guard rails, all enforced here (not by the prompt):

  - only `allowed_domains` (and their subdomains) can be opened; leaving them mid-task is reverted;
  - the agent never types into password fields (logging in is the human's job);
  - clicks that look consequential (delete / buy / pay / send / publish / credits / log out ...) need a
    human approval through request_approval first, then `confirm: true`;
  - every download and saved table lands in the mission outputs folder (readable with read_file) and is
    recorded as evidence, as are page visits and actions.

Playwright's sync API runs on one dedicated worker thread per session, so it works under any event loop
(uvicorn on Windows included). One profile is used by one task at a time (a process-wide lock).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from playwright.sync_api import Error as PWError
except ImportError:  # Playwright missing: tools report it; nothing below runs
    class PWError(Exception):  # type: ignore[no-redef]
        pass

from ..core import paths
from ..core.models import AgentDefinition, Attachment, BrowserConfig
from ..core.registry import expand_env
from .files import FileAccessError, _size, download_url, render_deliverable

NAMES = (
    "browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_select", "browser_press",
    "browser_scroll", "browser_wait", "browser_back", "browser_tables", "browser_download",
)
# Clicks whose target text matches this need a human approval first (then confirm=true).
RISKY = re.compile(
    r"\b(delete|eliminar|borrar|buy|comprar|purchase|checkout|pay|pagar|subscribe|suscrib\w*|upgrade|"
    r"contratar|send|enviar|publish|publicar|share|compartir|log ?out|sign ?out|cerrar sesi[oó]n|salir|"
    r"cr[eé]ditos?|credits?)\b",
    re.IGNORECASE,
)
RISKY_HREF = re.compile(r"log-?out|sign-?out|logoff|cerrar-?sesion", re.IGNORECASE)
DEFAULT_TEXT_CHARS = 8_000
MAX_TEXT_CHARS = 30_000
DEFAULT_ELEMENTS = 150
MAX_ELEMENTS = 400
DOWNLOAD_WAIT_S = 90
ACTION_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 45_000
_REF = re.compile(r"^(?:f(\d+))?e(\d+)$")


class BrowserError(Exception):
    """A problem the agent should read (bad ref, blocked domain, not configured ...)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ResolvedBrowser:
    agent_id: str
    profile: str
    start_url: str
    domains: list[str]
    max_turns: int

    @property
    def profile_dir(self) -> Path:
        return profile_dir(self.profile)

    @property
    def problem(self) -> str | None:
        """Why the browser can't be used (None = usable)."""
        if not self.domains:
            return (f"no allowed_domains for {self.agent_id}'s browser (set them in its YAML or through the "
                    "${...} variable it references in .env)")
        return None


def profile_dir(profile: str) -> Path:
    return paths.local_dir() / "browser" / paths.safe_name(profile)


def _host(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    host = urlparse(value).hostname if "://" in value else value.split("/")[0].split(":")[0]
    host = (host or "").strip(".")
    return host.removeprefix("www.")


def resolve(agent_id: str, cfg: BrowserConfig) -> ResolvedBrowser:
    start = expand_env(cfg.start_url).strip()
    if "${" in start:
        start = ""
    domains: list[str] = []
    for raw in cfg.allowed_domains:
        for part in expand_env(raw).split(","):
            host = "" if "${" in part else _host(part)
            if host and host not in domains:
                domains.append(host)
    if not domains and start:
        domains = [_host(start)]
    return ResolvedBrowser(agent_id, cfg.profile, start, domains, cfg.max_turns)


def host_allowed(url: str, domains: list[str]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def playwright_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def headless() -> bool:
    return os.getenv("ATLAS_BROWSER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def channel() -> str:
    return os.getenv("ATLAS_BROWSER_CHANNEL", "chrome").strip().lower() or "chrome"


def lock_wait_s() -> float:
    try:
        return max(1.0, float(os.getenv("ATLAS_BROWSER_LOCK_WAIT", "900")))
    except ValueError:
        return 900.0


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _profile_lock(profile: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(profile, threading.Lock())


def _proactor_on_windows() -> None:
    """Playwright starts the browser as a subprocess from its own event loop, which on Windows needs the Proactor
    loop. uvicorn --reload switches the process to the Selector policy; switch new loops back (the server's
    running loop is unaffected)."""
    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        if type(policy).__name__ == "WindowsSelectorEventLoopPolicy":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]


def launch_context(pw: Any, profile: str, *, show: bool) -> Any:
    """A persistent context on the profile folder: installed Chrome (ATLAS_BROWSER_CHANNEL, default chrome),
    falling back to Playwright's bundled Chromium."""
    from playwright.sync_api import Error as PlaywrightError

    folder = profile_dir(profile)
    folder.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"headless": not show, "accept_downloads": True}
    if show:
        kwargs["no_viewport"] = True
    else:
        kwargs["viewport"] = {"width": 1440, "height": 900}
    exe = os.getenv("ATLAS_BROWSER_EXECUTABLE", "").strip()
    if exe:  # an explicit browser binary (portable Chrome, a pinned Chromium)
        kwargs["executable_path"] = os.path.expanduser(exe)
    wanted = "chromium" if exe else channel()
    tries = [wanted, "chromium"] if wanted != "chromium" else ["chromium"]
    last: Exception | None = None
    for ch in tries:
        try:
            opts = dict(kwargs)
            if ch != "chromium":
                opts["channel"] = ch
            return pw.chromium.launch_persistent_context(str(folder), **opts)
        except PlaywrightError as exc:
            last = exc
            text = str(exc)
            if "already in use" in text or "ProcessSingleton" in text or "SingletonLock" in text:
                raise BrowserError(
                    f"the '{profile}' browser profile is open elsewhere (a login window or another ATLAS task); "
                    "close that window and try again") from None
    raise BrowserError(
        "could not start a browser: install Google Chrome (or set ATLAS_BROWSER_CHANNEL=msedge), or run "
        f"`uv run playwright install chromium` — {str(last).splitlines()[0] if last else ''}")


# ---------------------------------------------------------------------------
# Page scripts
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""
([prefix, limit, filter]) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],' +
    '[role=tab],[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=switch],[role=combobox],' +
    '[role=treeitem],[role=gridcell][tabindex],[onclick],[contenteditable=true]';
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const labelOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
    const byId = el.getAttribute('aria-labelledby');
    const lab = byId ? clean(byId.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || '').join(' ')) : '';
    return clean(el.getAttribute('aria-label') || lab || (el.labels && el.labels[0] ? el.labels[0].innerText : '') ||
      (isField ? '' : el.innerText) || el.getAttribute('placeholder') || el.getAttribute('title') ||
      el.getAttribute('alt') || ((el.querySelector && el.querySelector('img[alt]')) || {}).alt ||
      el.getAttribute('name') || (tag === 'input' && ['button', 'submit'].includes(el.type) ? el.value : '') || '').slice(0, 90);
  };
  document.querySelectorAll('[data-atlas-ref]').forEach((e) => e.removeAttribute('data-atlas-ref'));
  const want = (filter || '').toLowerCase();
  const items = [];
  let n = 0, total = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    n += 1;
    const ref = prefix + n;
    el.setAttribute('data-atlas-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'select' :
      tag === 'textarea' ? 'textbox' : tag === 'input' ? (['checkbox', 'radio', 'button', 'submit', 'file',
      'password', 'date', 'number', 'search'].includes(type) ? type : 'textbox') : tag);
    let line = `[${ref}] ${role} "${labelOf(el)}"`;
    if (tag === 'input' || tag === 'textarea') {
      if (type === 'password') line += ' (password: the human logs in, never type here)';
      else if (type === 'checkbox' || type === 'radio') line += el.checked ? ' [checked]' : ' [unchecked]';
      else if (!['button', 'submit'].includes(type) && el.value) line += ` value="${clean(el.value).slice(0, 60)}"`;
    } else if (tag === 'select') {
      const opts = [...el.options];
      const sel = opts.filter((o) => o.selected).map((o) => clean(o.text)).join(', ');
      line += ` selected="${sel.slice(0, 60)}" options: ` + opts.slice(0, 20).map((o) => clean(o.text).slice(0, 30)).join(' | ') +
        (opts.length > 20 ? ` | … (${opts.length} total)` : '');
    } else if (el.getAttribute('aria-checked') !== null) {
      line += el.getAttribute('aria-checked') === 'true' ? ' [checked]' : ' [unchecked]';
    }
    if (el.getAttribute('aria-expanded') !== null) line += el.getAttribute('aria-expanded') === 'true' ? ' [expanded]' : ' [collapsed]';
    if (el.getAttribute('aria-selected') === 'true') line += ' [selected]';
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') line += ' [disabled]';
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      if (href && !href.startsWith('javascript')) line += ` → ${href.slice(0, 80)}`;
    }
    if (want && !line.toLowerCase().includes(want)) continue;
    total += 1;
    if (items.length < limit) items.push(line);
  }
  return {items, total, all: n};
}
"""

_TABLES_JS = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = [];
  for (const t of document.querySelectorAll('table')) {
    if (!vis(t)) continue;
    const rows = [...t.rows].map((r) => [...r.cells].map((c) => clean(c.innerText)));
    if (rows.length && rows.some((r) => r.some((c) => c))) out.push({kind: 'table', rows});
  }
  for (const g of document.querySelectorAll('[role=grid],[role=table],[role=treegrid]')) {
    if (!vis(g) || g.tagName.toLowerCase() === 'table') continue;
    const rows = [...g.querySelectorAll('[role=row]')].map((r) =>
      [...r.querySelectorAll('[role=columnheader],[role=rowheader],[role=cell],[role=gridcell]')].map((c) => clean(c.innerText)));
    if (rows.length && rows.some((r) => r.some((c) => c))) out.push({kind: 'grid', rows});
  }
  return out;
}
"""

_LABEL_JS = r"""
(el) => [(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') ||
  el.getAttribute('title') || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim().slice(0, 200),
  el.getAttribute('href') || '']
"""

_NUMBER = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


def _cell(value: str) -> Any:
    v = value.strip()
    if _NUMBER.match(v):
        n = float(v.replace(",", ""))
        return int(n) if n.is_integer() and "." not in v else n
    return v


def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class BrowserResult:
    text: str
    ok: bool = True
    evidence: list[tuple[str, str, str, bool]] = field(default_factory=list)  # (kind, ref, detail, ok)
    attachments: list[Attachment] = field(default_factory=list)


def _save_unique(folder: Path, name: str, write: Any) -> Path:
    """Create folder/name without overwriting ('name (2).ext'); `write(path)` writes the file."""
    folder.mkdir(parents=True, exist_ok=True)
    name = paths.safe_name(name)
    stem, ext = Path(name).stem, Path(name).suffix
    candidate, n = name, 2
    while (folder / candidate).exists():
        candidate = f"{stem} ({n}){ext}"
        n += 1
        if n > 999:
            raise FileAccessError("too many files with this name")
    target = folder / candidate
    write(target)
    return target


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class BrowserSession:
    """One agent task's browser. Every Playwright call runs on this session's own worker thread."""

    def __init__(self, cfg: ResolvedBrowser, *, outputs: Path, mission_id: str):
        self.cfg = cfg
        self.outputs = outputs
        self.mission_id = mission_id
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"atlas-browser-{cfg.profile}")
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._lock: threading.Lock | None = None
        self._downloads: list[Any] = []
        self._seen_downloads = 0
        self._visited: set[str] = set()
        self.closed = False

    # -- plumbing --------------------------------------------------------------

    async def run(self, name: str, args: dict[str, Any], *, approved: bool = False) -> BrowserResult:
        if self.closed:
            return BrowserResult("The browser session is closed.", ok=False)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._dispatch, name, args, approved)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._pool, self._shutdown)
        finally:
            self._pool.shutdown(wait=False)

    def _shutdown(self) -> None:
        with contextlib.suppress(Exception):
            if self._ctx is not None:
                self._ctx.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                self._pw.stop()
        self._ctx = self._pw = self._page = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def _dispatch(self, name: str, args: dict[str, Any], approved: bool) -> BrowserResult:
        try:
            self._ensure()
            fn = getattr(self, "_" + name.removeprefix("browser_"), None)
            if fn is None or name not in NAMES:
                return BrowserResult(f"Unknown browser tool '{name}'.", ok=False)
            return fn(args, approved)
        except BrowserError as exc:
            return BrowserResult(f"Error: {exc}", ok=False)
        except PWError as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            if "Timeout" in first:
                first += " — the element may be hidden, covered or not ready: call browser_snapshot and retry"
            return BrowserResult(f"Browser error: {first}", ok=False)

    def _ensure(self) -> None:
        if self._ctx is not None:
            return
        problem = self.cfg.problem
        if problem:
            raise BrowserError(problem)
        if not playwright_installed():
            raise BrowserError("Playwright is not installed on this machine (uv sync installs it)")
        lock = _profile_lock(self.cfg.profile)
        if not lock.acquire(timeout=lock_wait_s()):
            raise BrowserError("the browser profile stayed busy with another task; try again later")
        self._lock = lock
        try:
            from playwright.sync_api import sync_playwright

            _proactor_on_windows()
            self._pw = sync_playwright().start()
            self._ctx = launch_context(self._pw, self.cfg.profile, show=not headless())
        except BaseException:
            self._shutdown()
            raise
        self._ctx.set_default_timeout(ACTION_TIMEOUT_MS)
        self._ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._ctx.on("page", self._adopt)
        for p in self._ctx.pages:
            p.on("download", self._on_download)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def _on_download(self, download: Any) -> None:
        self._downloads.append(download)

    def _adopt(self, page: Any) -> None:
        """A new tab or popup (an export often opens one): follow it."""
        page.on("download", self._on_download)
        self._page = page

    @property
    def page(self) -> Any:
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self._ctx.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else self._ctx.new_page()
        return self._page

    def _settle(self, ms: int = 700) -> None:
        with contextlib.suppress(PWError):
            self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
        self.page.wait_for_timeout(ms)

    def _where(self) -> str:
        p = self.page
        try:
            title = p.title()
        except PWError:
            title = ""
        return f"{p.url}" + (f" — {_short(title, 80)}" if title else "")

    def _guard_domain(self, res: BrowserResult) -> None:
        """After any action: if the current tab left the allowed domains, go back (or close it)."""
        url = self.page.url
        if url in ("about:blank", "") or host_allowed(url, self.cfg.domains):
            return
        bad = url
        page = self.page
        if len([p for p in self._ctx.pages if not p.is_closed()]) > 1:
            page.close()
            self._page = None
        else:
            try:
                page.go_back(timeout=10_000)
            except PWError:
                page.goto("about:blank")
        res.ok = False
        res.text += (f"\nBLOCKED: that led to {_short(bad, 100)}, outside the allowed sites "
                     f"({', '.join(self.cfg.domains)}). Went back. If it was a login page, the session expired: "
                     "stop and say the human must run `atlas-browser login` again.")
        res.evidence.append(("browser_visit", bad, "blocked: outside the allowed domains", False))

    def _visit(self, res: BrowserResult, detail: str) -> None:
        url = self.page.url
        if url not in self._visited and url != "about:blank":
            self._visited.add(url)
            res.evidence.append(("browser_visit", url, detail, True))

    def _locate(self, ref: Any) -> tuple[Any, str]:
        ref = str(ref or "").strip().strip("[]")
        m = _REF.match(ref)
        if not m:
            raise BrowserError(f"'{ref}' is not an element ref: use one like e12 from browser_snapshot")
        frames = self.page.frames
        idx = int(m.group(1) or 0)
        if idx >= len(frames):
            raise BrowserError(f"ref {ref} is stale: call browser_snapshot again")
        loc = frames[idx].locator(f'[data-atlas-ref="{ref}"]')
        if loc.count() == 0:
            raise BrowserError(f"ref {ref} is not on the page anymore (it changed): call browser_snapshot again")
        return loc.first, ref

    def _label(self, loc: Any) -> tuple[str, str]:
        """(visible label, href) of an element."""
        try:
            text, href = loc.evaluate(_LABEL_JS)
            return str(text or ""), str(href or "")
        except PWError:
            return "", ""

    def _check_risky(self, loc: Any, approved: bool, confirm: bool) -> str:
        text, href = self._label(loc)
        risky = bool(RISKY.search(text) or (href and RISKY_HREF.search(href)))
        if risky and not (confirm and approved):
            raise BrowserError(
                f"'{_short(text, 60)}' looks consequential (it may delete, spend credits, buy, send, "
                "publish or log out). Call request_approval with proposed_action describing exactly this click; "
                "if the human approves, call this tool again with confirm: true.")
        return _short(text, 60)

    def _new_downloads(self, wait_s: float) -> list[Any]:
        deadline = time.monotonic() + wait_s
        while True:
            if len(self._downloads) > self._seen_downloads:
                # give a multi-file export a moment to start the rest
                self.page.wait_for_timeout(500)
                new = self._downloads[self._seen_downloads:]
                self._seen_downloads = len(self._downloads)
                return new
            if time.monotonic() >= deadline:
                return []
            self.page.wait_for_timeout(250)

    def _save_downloads(self, res: BrowserResult, downloads: list[Any], wanted: str = "") -> None:
        for i, d in enumerate(downloads):
            failure = d.failure()
            suggested = d.suggested_filename or "download"
            if failure:
                res.evidence.append(("browser_download", suggested, f"failed: {failure}", False))
                res.text += f"\nDownload of {suggested} failed: {failure}"
                res.ok = False
                continue
            name = suggested
            if wanted and i == 0:
                ext = Path(suggested).suffix
                name = wanted if Path(wanted).suffix else wanted + ext
            target = _save_unique(self.outputs, name, d.save_as)
            size = target.stat().st_size
            att = Attachment(name=target.name, kind="file", size_bytes=size,
                             download_url=download_url(self.mission_id, target.name))
            res.attachments.append(att)
            res.evidence.append(("browser_download", str(target), f"{_size(size)} from {_short(d.url, 90)}", True))
            res.text += (f"\nDownloaded {target.name} ({_size(size)}) into the mission outputs: {target}\n"
                         "Read it with read_file (xlsx, csv, pdf, docx work).")

    def _after_action(self, res: BrowserResult, *, wait_downloads: float = 1.0) -> BrowserResult:
        self._settle()
        new = self._new_downloads(wait_downloads)
        if new:
            self._save_downloads(res, new)
        self._guard_domain(res)
        res.text += f"\nNow at: {self._where()}\nCall browser_snapshot to see the page."
        return res

    # -- tools -----------------------------------------------------------------

    def _open(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        url = str(args.get("url") or self.cfg.start_url or "").strip()
        if not url:
            raise BrowserError("give a url (no start_url is configured)")
        if "://" not in url:
            url = "https://" + url
        if not host_allowed(url, self.cfg.domains):
            return BrowserResult(
                f"Error: {url} is outside the allowed sites ({', '.join(self.cfg.domains)}).", ok=False,
                evidence=[("browser_visit", url, "blocked: outside the allowed domains", False)])
        self.page.goto(url, wait_until="domcontentloaded")
        self._settle(1200)
        res = BrowserResult(f"Opened {self._where()}")
        self._guard_domain(res)
        if res.ok:
            self._visit(res, _short(self.page.title(), 100))
            res.text += "\nCall browser_snapshot to read it and get element refs."
        return res

    def _snapshot(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        page = self.page
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            max_chars = min(MAX_TEXT_CHARS, max(500, int(args.get("max_chars") or DEFAULT_TEXT_CHARS)))
        except (TypeError, ValueError):
            max_chars = DEFAULT_TEXT_CHARS
        try:
            limit = min(MAX_ELEMENTS, max(10, int(args.get("max_elements") or DEFAULT_ELEMENTS)))
        except (TypeError, ValueError):
            limit = DEFAULT_ELEMENTS
        flt = str(args.get("filter") or "").strip()
        texts: list[str] = []
        items: list[str] = []
        total = 0
        for i, frame in enumerate(page.frames):
            if frame.is_detached():
                continue
            if i and not host_allowed(frame.url, self.cfg.domains):
                continue  # ads / third-party widgets
            try:
                body = frame.locator("body").inner_text(timeout=5_000)
            except PWError:
                body = ""
            if body.strip():
                texts.append(body if i == 0 else f"\n[frame {i}]\n{body}")
            try:
                got = frame.evaluate(_SNAPSHOT_JS, ["e" if i == 0 else f"f{i}e", limit - len(items), flt])
            except PWError:
                continue
            items += got.get("items", [])
            total += int(got.get("total", 0))
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(texts)).strip()
        chunk = text[offset: offset + max_chars]
        end = offset + len(chunk)
        lines = [f"URL: {page.url}", f"Title: {page.title()}", ""]
        if args.get("elements_only"):
            lines.append(f"(page text: {len(text):,} chars, omitted)")
        else:
            more = f"; call browser_snapshot with offset={end} for more" if end < len(text) else ""
            lines += [f"Page text (chars {offset:,}-{end:,} of {len(text):,}{more}):", chunk or "(no text)"]
        lines.append("")
        shown = f"{len(items)} of {total}" + (f" matching '{flt}'" if flt else "")
        hint = "" if len(items) >= total else "; use filter='...' to find others"
        lines.append(f"Interactive elements ({shown}{hint}):")
        lines += items or ["(none)"]
        res = BrowserResult("\n".join(lines))
        self._visit(res, f"{_short(page.title(), 80)} · {len(text):,} chars")
        return res

    def _click(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        loc, ref = self._locate(args.get("ref"))
        label = self._check_risky(loc, approved, bool(args.get("confirm")))
        loc.click()
        res = BrowserResult(f'Clicked [{ref}] "{label}".',
                            evidence=[("browser_action", self.page.url, f'click "{label}"', True)])
        return self._after_action(res)

    def _type(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        loc, ref = self._locate(args.get("ref"))
        if str(loc.get_attribute("type") or "").lower() == "password":
            raise BrowserError("never type into password fields: logging in is the human's job "
                               "(`atlas-browser login`). Stop and report that the session needs a login.")
        text = str(args.get("text") if args.get("text") is not None else "")
        if args.get("clear", True):
            loc.fill(text)
        else:
            loc.press_sequentially(text, delay=30)
        label = _short(self._label(loc)[0] or ref, 50)
        res = BrowserResult(f'Typed "{_short(text, 60)}" into [{ref}].',
                            evidence=[("browser_action", self.page.url, f'typed "{_short(text, 60)}" into {label}', True)])
        if args.get("submit"):
            loc.press("Enter")
            res.text += " Pressed Enter."
            return self._after_action(res)
        return res

    def _select(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        loc, ref = self._locate(args.get("ref"))
        option = str(args.get("option") or "").strip()
        if not option:
            raise BrowserError("give the option to select (its visible text or value)")
        try:
            loc.select_option(label=option, timeout=5_000)
        except PWError:
            loc.select_option(value=option, timeout=5_000)
        res = BrowserResult(f'Selected "{option}" in [{ref}].',
                            evidence=[("browser_action", self.page.url, f'selected "{_short(option, 60)}"', True)])
        return self._after_action(res)

    def _press(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        key = str(args.get("key") or "").strip()
        if not key:
            raise BrowserError("give a key, e.g. Enter, Escape, Tab, PageDown")
        self.page.keyboard.press(key)
        res = BrowserResult(f"Pressed {key}.", evidence=[("browser_action", self.page.url, f"key {key}", True)])
        return self._after_action(res)

    def _scroll(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        if args.get("ref"):
            loc, ref = self._locate(args.get("ref"))
            loc.scroll_into_view_if_needed()
            return BrowserResult(f"Scrolled [{ref}] into view.")
        down = str(args.get("direction") or "down").lower() != "up"
        self.page.mouse.wheel(0, 900 if down else -900)
        self.page.wait_for_timeout(600)
        return BrowserResult(f"Scrolled {'down' if down else 'up'}. Call browser_snapshot to see what loaded.")

    def _wait(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        try:
            secs = min(60.0, max(0.5, float(args.get("seconds") or 3)))
        except (TypeError, ValueError):
            secs = 3.0
        text = str(args.get("text") or "").strip()
        if text:
            try:
                self.page.get_by_text(text).first.wait_for(timeout=secs * 1000)
            except PWError:
                return BrowserResult(f"'{_short(text, 60)}' did not appear within {secs:g} s.", ok=False)
            res = BrowserResult(f"'{_short(text, 60)}' is on the page.")
        else:
            self.page.wait_for_timeout(secs * 1000)
            res = BrowserResult(f"Waited {secs:g} s.")
        new = self._new_downloads(0.2)
        if new:
            self._save_downloads(res, new)
        return res

    def _back(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        self.page.go_back()
        return self._after_action(BrowserResult("Went back."))

    def _tables(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        tables: list[dict[str, Any]] = []
        for i, frame in enumerate(self.page.frames):
            if frame.is_detached() or (i and not host_allowed(frame.url, self.cfg.domains)):
                continue
            try:
                tables += frame.evaluate(_TABLES_JS)
            except PWError:
                continue
        if not tables:
            return BrowserResult("No tables or grids are visible on this page. (Charts and maps are not tables: "
                                 "look for an export / download button instead.)", ok=False)
        index = args.get("index")
        picked = list(enumerate(tables, 1))
        if index not in (None, "", 0):
            try:
                k = int(index)
            except (TypeError, ValueError):
                raise BrowserError("index must be a table number from the list") from None
            if not 1 <= k <= len(tables):
                raise BrowserError(f"there are {len(tables)} tables; index must be 1-{len(tables)}")
            picked = [(k, tables[k - 1])]
        lines = [f"{len(tables)} table(s) on {self.page.url}:"]
        for k, t in picked:
            rows = t["rows"]
            cols = max((len(r) for r in rows), default=0)
            lines.append(f"\n#{k} ({t['kind']}, {len(rows)} rows x {cols} cols)")
            for r in rows[:8]:
                lines.append(" | ".join(_short(c, 30) for c in r))
            if len(rows) > 8:
                lines.append(f"… {len(rows) - 8} more rows")
        if any(t["kind"] == "grid" for _, t in picked):
            lines.append("\nNote: grids often render only the visible rows; prefer the site's export for full data.")
        res = BrowserResult("\n".join(lines))
        self._visit(res, f"{len(tables)} tables")
        save_as = str(args.get("save_as") or "").strip()
        if save_as:
            fmt = (str(args.get("format") or "").lower().lstrip(".") or Path(save_as).suffix.lower().lstrip(".")
                   or "xlsx")
            if fmt not in ("xlsx", "csv"):
                raise BrowserError("format must be xlsx or csv")
            if fmt == "csv" and len(picked) > 1:
                raise BrowserError("csv holds one table: pass index, or use xlsx for all of them")
            sheets = {f"Table {k}": [[_cell(c) for c in r] for r in t["rows"]] for k, t in picked}
            data = render_deliverable(fmt, None, sheets)
            name = save_as if Path(save_as).suffix.lower() == "." + fmt else f"{Path(save_as).stem or 'tables'}.{fmt}"
            target = _save_unique(self.outputs, name, lambda p: p.write_bytes(data))
            att = Attachment(name=target.name, kind="file", size_bytes=len(data),
                             download_url=download_url(self.mission_id, target.name))
            res.attachments.append(att)
            nrows = sum(len(t["rows"]) for _, t in picked)
            res.evidence.append(("file_written", str(target), f"{_size(len(data))}, {nrows} rows from {_short(self.page.url, 80)}", True))
            res.text += f"\n\nSaved {target.name} ({_size(len(data))}) to the mission outputs: {target}"
        return res

    def _download(self, args: dict[str, Any], approved: bool) -> BrowserResult:
        loc, ref = self._locate(args.get("ref"))
        label = self._check_risky(loc, approved, bool(args.get("confirm")))
        self._seen_downloads = len(self._downloads)
        loc.click()
        res = BrowserResult(f'Clicked [{ref}] "{label}" and waited for a download.',
                            evidence=[("browser_action", self.page.url, f'download via "{label}"', True)])
        try:
            wait = min(300.0, max(5.0, float(args.get("wait_seconds") or DOWNLOAD_WAIT_S)))
        except (TypeError, ValueError):
            wait = float(DOWNLOAD_WAIT_S)
        new = self._new_downloads(wait)
        if new:
            self._save_downloads(res, new, wanted=paths.safe_name(str(args.get("filename") or "")) if args.get("filename") else "")
        else:
            res.ok = False
            res.text += (f"\nNo download started within {wait:g} s. The export may need another step (a dialog "
                         "with a confirm / format choice: snapshot and use browser_download on that button), may be "
                         "prepared in the background (wait, then look for a 'download ready' link) or sent by email.")
        self._settle(300)
        self._guard_domain(res)
        res.text += f"\nNow at: {self._where()}"
        return res


# ---------------------------------------------------------------------------
# Tool activity lines
# ---------------------------------------------------------------------------


def activity(name: str, args: dict[str, Any]) -> str:
    if name == "browser_open":
        return _short(f"Browser · opening {_host(str(args.get('url') or '')) or 'the start page'}", 90)
    return {
        "browser_snapshot": "Browser · reading the page",
        "browser_click": f"Browser · clicking {args.get('ref', '')}",
        "browser_type": "Browser · filling a field",
        "browser_select": f"Browser · selecting {_short(str(args.get('option', '')), 40)}",
        "browser_press": f"Browser · pressing {args.get('key', '')}",
        "browser_scroll": "Browser · scrolling",
        "browser_wait": "Browser · waiting for the page",
        "browser_back": "Browser · going back",
        "browser_tables": "Browser · extracting tables",
        "browser_download": "Browser · downloading an export",
    }.get(name, "Browser")


def browser_note(cfg: ResolvedBrowser) -> str:
    """Part of the task message for agents with a browser."""
    start = f" Start page: {cfg.start_url}." if cfg.start_url else ""
    return (
        "Browser (browser_* tools): a dedicated browser already signed into "
        f"{', '.join(cfg.domains) or '(no sites configured)'} — only those sites open.{start}\n"
        "- Loop: browser_open → browser_snapshot (page text + element refs like e12) → browser_click / "
        "browser_type / browser_select → browser_snapshot again. Refs change when the page changes.\n"
        "- Data: browser_tables reads (and with save_as saves) visible tables; for full datasets use the site's "
        "export with browser_download — the file lands in your outputs folder; then read_file it.\n"
        "- If you land on a login page, the session expired: do NOT try to log in. Stop and say the human must "
        "run `atlas-browser login` again.\n"
        "- Clicks that delete, spend credits, buy, send, publish or log out need request_approval first, then "
        "confirm: true. Record what you did in the site in actions_taken."
    )


# ---------------------------------------------------------------------------
# CLI: one-time login and status
# ---------------------------------------------------------------------------


def _agents() -> list[AgentDefinition]:
    from ..core.registry import AgentRegistry

    return [a for a in AgentRegistry.load().all() if a.browser is not None]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(paths.REPO_ROOT / ".env", override=False)


def login(agent_id: str, url: str | None = None) -> int:
    agents = {a.id: a for a in _agents()}
    agent = agents.get(agent_id) or next((a for a in agents.values() if a.name.lower() == agent_id.lower()), None)
    if agent is None or agent.browser is None:
        print(f"No agent '{agent_id}' with a browser. Agents with one: {', '.join(agents) or 'none'}")
        return 2
    cfg = resolve(agent.id, agent.browser)
    target = url or cfg.start_url or (f"https://{cfg.domains[0]}" if cfg.domains else "")
    if not target:
        print(f"{agent.id}: set browser.start_url / allowed_domains (see docs/BROWSER.md)")
        return 2
    if not playwright_installed():
        print("Playwright is not installed: run `uv sync` in apps/api")
        return 2
    from playwright.sync_api import sync_playwright

    _proactor_on_windows()
    print(f"Opening {agent.name}'s browser (profile '{cfg.profile}' in {cfg.profile_dir}).")
    print("Sign in to the site (tick 'remember me' if it offers it), then CLOSE the browser window to save.")
    with sync_playwright() as pw:
        try:
            ctx = launch_context(pw, cfg.profile, show=True)
        except BrowserError as exc:
            print(f"Error: {exc}")
            return 1
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(target)
        ctx.wait_for_event("close", timeout=0)
    print("Saved. ATLAS missions will reuse this session until the site logs it out.")
    return 0


def status() -> int:
    agents = _agents()
    if not agents:
        print("No agent has a browser block.")
        return 0
    for a in agents:
        assert a.browser is not None
        cfg = resolve(a.id, a.browser)
        folder = cfg.profile_dir
        state = "profile saved" if folder.exists() and any(folder.iterdir()) else "not logged in yet"
        print(f"{a.id} ({a.name}): profile '{cfg.profile}' · {state} · sites: {', '.join(cfg.domains) or 'NONE'}"
              + (f" · start {cfg.start_url}" if cfg.start_url else "") + (f" · {cfg.problem}" if cfg.problem else ""))
    print(f"Playwright installed: {'yes' if playwright_installed() else 'NO (uv sync)'} · channel: {channel()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(prog="atlas-browser", description="ATLAS agents' dedicated browser profiles")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="open the agent's browser so you can sign in once")
    p_login.add_argument("agent", help="agent id (e.g. market-studies) or name (MERCATO)")
    p_login.add_argument("--url", help="page to open instead of the start_url")
    sub.add_parser("status", help="agents with a browser and whether their profile is signed in")
    args = parser.parse_args(argv)
    if args.cmd == "login":
        return login(args.agent, args.url)
    return status()


if __name__ == "__main__":
    sys.exit(main())
