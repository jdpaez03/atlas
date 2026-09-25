"""Dedicated agent browser (docs/BROWSER.md): config, guard rails and a real browser against a local fake
market-study site (login cookie, filters, a table, an export download, a consequential button, an off-site link).

The browser tests launch a real headless Chromium/Chrome and are skipped when none can start."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from atlas.core import paths
from atlas.core.events import EventBus
from atlas.core.models import BrowserConfig
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore
from atlas.live import AgentLoader, FakeLLM, LiveConfig, LiveEngine, ModelConfig, NodeContext, PriceTable
from atlas.live import browser as B
from atlas.live.llm import call_text, tool_use
from atlas.live.sdk import FakeClaudeSDK, call, say

ROWS = {
    "Monterrey": [("Torre MTY", "45,500", "120"), ("Vista Norte", "38,200", "86")],
    "San Pedro": [("Torre SP", "72,300", "64"), ("Valle Alto", "68,900", "40")],
}


class Site(BaseHTTPRequestHandler):
    """A tiny logged-in market-study site on 127.0.0.1 (localhost counts as ANOTHER site)."""

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: str = "", headers: dict[str, str] | None = None, ctype="text/html"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        logged = "sid=ok" in (self.headers.get("Cookie") or "")
        port = self.server.server_address[1]
        if url.path == "/auto-login":  # what the human's one-time login leaves behind: a persistent cookie
            return self._send(302, headers={"Set-Cookie": "sid=ok; Max-Age=86400; Path=/", "Location": "/estudio"})
        if url.path == "/login":
            return self._send(200, "<h1>Iniciar sesión</h1><input name=u aria-label=Usuario>"
                                   "<input type=password name=p aria-label=Contraseña><button>Entrar</button>")
        if not logged:
            return self._send(302, headers={"Location": "/login"})
        if url.path == "/estudio":
            mun = q.get("mun", "Monterrey")
            rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in ROWS.get(mun, []))
            opts = "".join(f"<option{' selected' if m == mun else ''}>{m}</option>" for m in ROWS)
            return self._send(200, f"""<html><head><title>Estudio · {mun}</title></head><body>
<h1>Estudio de mercado</h1>
<form action="/estudio" method="get">
  <label for="mun">Municipio</label><select id="mun" name="mun">{opts}</select>
  <input name="q" placeholder="Buscar desarrollo">
  <button type="submit">Filtrar</button>
</form>
<table><tr><th>Desarrollo</th><th>Precio m2</th><th>Unidades</th></tr>{rows}</table>
<a href="/export.csv?mun={mun}">Exportar Excel</a>
<button onclick="document.getElementById('status').innerText='Búsqueda eliminada'">Eliminar búsqueda</button>
<div id="status"></div>
<a href="http://localhost:{port}/estudio">Ver en otro sitio</a>
<input type="password" aria-label="PIN">
</body></html>""")
        if url.path == "/export.csv":
            mun = q.get("mun", "Monterrey")
            body = "desarrollo,precio_m2,unidades\n" + "".join(
                f"{a},{b.replace(',', '')},{c}\n" for a, b, c in ROWS.get(mun, []))
            return self._send(200, body, ctype="text/csv", headers={
                "Content-Disposition": f'attachment; filename="comparables_{mun.replace(" ", "_")}.csv"'})
        return self._send(404, "not found")


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def browser_env(monkeypatch):
    """Headless; the container's pinned Chromium when present, else installed Chrome / bundled Chromium."""
    monkeypatch.setenv("ATLAS_BROWSER_HEADLESS", "1")
    if Path("/opt/pw-browsers/chromium").exists():
        monkeypatch.setenv("ATLAS_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium")
    if not B.playwright_installed():
        pytest.skip("Playwright not installed")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            B.launch_context(pw, "probe", show=False).close()
    except B.BrowserError as exc:
        pytest.skip(f"no browser can start here: {exc}")


def human_login(profile: str, site: str) -> None:
    """The one-time human login, into the agent's dedicated profile."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = B.launch_context(pw, profile, show=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{site}/auto-login")
        assert "Estudio de mercado" in page.content()
        ctx.close()


def ref_of(snapshot: str, pattern: str) -> str:
    m = re.search(r"\[(\w+)\] " + pattern, snapshot)
    assert m, f"{pattern!r} not in:\n{snapshot}"
    return m.group(1)


# ---------------------------------------------------------------------------
# config and guard rails (no browser)
# ---------------------------------------------------------------------------


def test_resolve_domains_from_env_and_start_url(monkeypatch):
    monkeypatch.setenv("MKT_DOMAINS", "https://www.Datos-Mercado.mx/app, portal.otro.com")
    monkeypatch.setenv("MKT_URL", "https://app.datos-mercado.mx/estudios")
    cfg = B.resolve("mercato", BrowserConfig(profile="mercato", start_url="${MKT_URL}",
                                             allowed_domains=["${MKT_DOMAINS}"]))
    assert cfg.domains == ["datos-mercado.mx", "portal.otro.com"] and cfg.problem is None
    assert cfg.start_url == "https://app.datos-mercado.mx/estudios"
    assert B.host_allowed("https://app.datos-mercado.mx/x", cfg.domains)
    assert B.host_allowed("https://datos-mercado.mx", cfg.domains)
    assert not B.host_allowed("https://datos-mercado.mx.evil.com", cfg.domains)
    assert not B.host_allowed("https://otro.com", cfg.domains)
    assert not B.host_allowed("file:///etc/passwd", cfg.domains)
    # no domains: the start_url's host; neither: unusable (unset variables don't leak through)
    only_url = B.resolve("m", BrowserConfig(profile="m", start_url="https://www.site.com/a"))
    assert only_url.domains == ["site.com"]
    monkeypatch.delenv("NOPE", raising=False)
    empty = B.resolve("m", BrowserConfig(profile="m", start_url="${NOPE}", allowed_domains=["${NOPE}"]))
    assert empty.domains == [] and empty.start_url == "" and "allowed_domains" in (empty.problem or "")


def test_risky_words():
    for label in ("Eliminar búsqueda", "Comprar reporte", "Descargar (3 créditos)", "Cerrar sesión", "Delete",
                  "Enviar a cliente", "Log out"):
        assert B.RISKY.search(label), label
    for label in ("Exportar Excel", "Filtrar", "Buscar", "Descargar reporte", "Siguiente", "Ver detalle"):
        assert not B.RISKY.search(label), label


def test_tool_specs_match_the_session_methods():
    from atlas.live.prompts import BROWSER_TOOLS

    assert tuple(t["name"] for t in BROWSER_TOOLS) == B.NAMES
    for name in B.NAMES:
        assert callable(getattr(B.BrowserSession, "_" + name.removeprefix("browser_")))


# ---------------------------------------------------------------------------
# the real browser
# ---------------------------------------------------------------------------


def test_session_against_a_logged_in_site(site, browser_env, tmp_path):
    human_login("mercato", site)
    cfg = B.resolve("mercato", BrowserConfig(profile="mercato", start_url=f"{site}/estudio",
                                             allowed_domains=["127.0.0.1"]))
    out = tmp_path / "outputs"

    async def go():
        s = B.BrowserSession(cfg, outputs=out, mission_id="msn_b1")
        r = {}
        r["open"] = await s.run("browser_open", {})
        r["snap"] = await s.run("browser_snapshot", {})
        snap = r["snap"].text
        await s.run("browser_select", {"ref": ref_of(snap, 'select "Municipio"'), "option": "San Pedro"})
        r["filter"] = await s.run("browser_click", {"ref": ref_of(snap, 'button "Filtrar"')})
        r["stale"] = await s.run("browser_click", {"ref": ref_of(snap, 'button "Filtrar"')})  # new page, old ref
        r["snap2"] = await s.run("browser_snapshot", {})
        snap2 = r["snap2"].text
        r["tables"] = await s.run("browser_tables", {"save_as": "comparables_sp"})
        r["download"] = await s.run("browser_download", {"ref": ref_of(snap2, 'link "Exportar Excel"')})
        snap3 = (await s.run("browser_snapshot", {"elements_only": True})).text
        delete = ref_of(snap3, 'button "Eliminar búsqueda"')
        r["risky"] = await s.run("browser_click", {"ref": delete})
        r["risky_confirm_no_approval"] = await s.run("browser_click", {"ref": delete, "confirm": True})
        r["risky_approved"] = await s.run("browser_click", {"ref": delete, "confirm": True}, approved=True)
        r["status"] = await s.run("browser_snapshot", {"max_chars": 2000})
        r["password"] = await s.run("browser_type", {"ref": ref_of(snap3, 'password "PIN"'), "text": "1234"})
        r["offsite"] = await s.run("browser_click", {"ref": ref_of(snap3, 'link "Ver en otro sitio"')})
        r["after_offsite"] = await s.run("browser_snapshot", {"elements_only": True})
        r["open_bad"] = await s.run("browser_open", {"url": "https://example.com"})
        await s.close()
        # the profile lock is released: a new session can use it right away
        s2 = B.BrowserSession(cfg, outputs=out, mission_id="msn_b1")
        r["reopen"] = await s2.run("browser_open", {"url": f"{site}/estudio?mun=Monterrey"})
        await s2.close()
        return r

    r = asyncio.run(go())
    assert r["open"].ok and "/estudio" in r["open"].text
    assert r["open"].evidence[0][0] == "browser_visit"
    snap = r["snap"].text
    assert "Estudio de mercado" in snap and "Torre MTY" in snap  # reused the human's login
    assert re.search(r'select "Municipio" selected="Monterrey" options: Monterrey \| San Pedro', snap)
    assert "(password: the human logs in, never type here)" in snap
    assert r["filter"].ok and "mun=San+Pedro" in r["filter"].text
    assert "Torre SP" in r["snap2"].text and "Torre MTY" not in r["snap2"].text

    import openpyxl

    t = r["tables"]
    assert t.ok and "#1 (table, 3 rows x 3 cols)" in t.text and t.attachments[0].name == "comparables_sp.xlsx"
    ws = openpyxl.load_workbook(out / "comparables_sp.xlsx").active
    assert [c.value for c in ws[2]] == ["Torre SP", 72300, 64]  # numbers are numbers
    assert t.evidence[-1][0] == "file_written"

    d = r["download"]
    assert d.ok and d.attachments[0].name == "comparables_San_Pedro.csv"
    assert (out / "comparables_San_Pedro.csv").read_text().startswith("desarrollo,precio_m2")
    assert d.attachments[0].download_url == "/missions/msn_b1/files/outputs/comparables_San_Pedro.csv"
    assert [e[0] for e in d.evidence] == ["browser_action", "browser_download"]

    assert not r["stale"].ok and "call browser_snapshot again" in r["stale"].text
    for key in ("risky", "risky_confirm_no_approval"):
        assert not r[key].ok and "request_approval" in r[key].text
    assert r["risky_approved"].ok and "Búsqueda eliminada" in r["status"].text
    assert not r["password"].ok and "never type into password" in r["password"].text
    off = r["offsite"]
    assert not off.ok and "BLOCKED" in off.text and ("browser_visit", ) == off.evidence[-1][:1]
    assert "URL: " + site in r["after_offsite"].text  # went back to the allowed site
    assert not r["open_bad"].ok and "outside the allowed sites" in r["open_bad"].text
    assert r["reopen"].ok and "Monterrey" in r["reopen"].text


def test_expired_session_lands_on_login_and_is_never_typed(site, browser_env, tmp_path):
    cfg = B.resolve("fresh", BrowserConfig(profile="fresh", start_url=f"{site}/estudio",
                                           allowed_domains=["127.0.0.1"]))

    async def go():
        s = B.BrowserSession(cfg, outputs=tmp_path, mission_id="m")
        await s.run("browser_open", {})
        snap = (await s.run("browser_snapshot", {})).text
        typed = await s.run("browser_type", {"ref": ref_of(snap, 'password "Contraseña"'), "text": "x"})
        await s.close()
        return snap, typed

    snap, typed = asyncio.run(go())
    assert "Iniciar sesión" in snap and not typed.ok


# ---------------------------------------------------------------------------
# inside a mission (both backends)
# ---------------------------------------------------------------------------

MODELS = ModelConfig(orchestrator="o", default="s", fast="f")
PLAN = [
    {"ref": "mkt", "title": "Comparables", "description": "Pull the San Pedro comparables from the market site",
     "assigned_to": "oracle"},
    {"ref": "sum", "title": "Summarize", "description": "Summarize", "assigned_to": "alfred"},
]
REPORT = {"asked_to": "mkt", "actions_taken": ["Exported comparables_San_Pedro.csv from the market site"],
          "inputs_used": ["comparables_San_Pedro.csv"],
          "findings": [{"kind": "FACT", "statement": "Torre SP sells at 72,300 per m2", "confidence": "HIGH"}],
          "confidence": "HIGH"}
SUMMARY = {"asked_to": "sum", "findings": [{"kind": "FACT", "statement": "ok", "confidence": "HIGH"}],
           "confidence": "HIGH"}
FINAL = {"executive_summary": "Done.", "objective_status": "ACHIEVED",
         "key_findings": [{"kind": "FACT", "statement": "ok", "confidence": "HIGH"}]}


def browser_registry(site: str) -> AgentRegistry:
    reg = AgentRegistry.load()
    cfg = BrowserConfig(profile="oracle-test", start_url=f"{site}/estudio?mun=San%20Pedro",
                        allowed_domains=["127.0.0.1"], max_turns=15)
    reg._agents["oracle"] = reg.get("oracle").model_copy(update={"browser": cfg})
    return reg


# On /estudio the element refs are, in page order: e1 select, e2 search box, e3 Filtrar, e4 Exportar Excel
STEPS = [("browser_open", {}), ("browser_snapshot", {}), ("browser_download", {"ref": "e4"}),
         ("read_file", {"path": "comparables_San_Pedro.csv"})]


def _check(store: WorldStore) -> None:
    snap = store.snapshot()
    rep = next(r for r in snap.agent_reports if r.agent_id == "oracle")
    kinds = [e.kind for e in rep.evidence]
    assert kinds == ["browser_visit", "browser_action", "browser_download", "file_read"], kinds
    assert rep.confidence == "HIGH" and not any(x.startswith("Unverified") for x in rep.limitations)
    assert [d.name for d in rep.deliverables] == ["comparables_San_Pedro.csv"]
    mid = snap.missions[0].id
    assert (paths.outputs_dir("corporate", mid) / "comparables_San_Pedro.csv").is_file()
    acts = [e.payload["state"].get("activity") for e in store.bus.history()
            if e.type == "agent.state_changed" and e.agent_id == "oracle"]
    assert "Browser · downloading an export" in acts


def test_browser_tools_in_a_live_mission_api(site, browser_env):
    human_login("oracle-test", site)
    reg = browser_registry(site)
    llm = FakeLLM()
    llm.when(lambda kw: "STAGE: PLANNING" in call_text(kw), tool_use("create_plan", {"tasks": PLAN}))
    llm.when(lambda kw: "Your task: Comparables" in call_text(kw),
             *[tool_use(n, a) for n, a in STEPS], tool_use("submit_report", REPORT))
    llm.when(lambda kw: "Your task: Summarize" in call_text(kw), tool_use("submit_report", SUMMARY))
    llm.when(lambda kw: "STAGE: REVIEW" in call_text(kw), tool_use("request_followups", {"tasks": []}))
    llm.when(lambda kw: "STAGE: CONSOLIDATION" in call_text(kw), tool_use("submit_mission_report", FINAL))

    async def go():
        store = WorldStore(reg, EventBus())
        engine = LiveEngine(store, loader=AgentLoader(reg, MODELS),
                            context=NodeContext(reg, local_dir=Path("/nonexistent")),
                            config=LiveConfig(models=MODELS, max_turns=4), llm=llm, prices=PriceTable())
        m = await engine.start("Comparables San Pedro", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 60)
        return store

    store = asyncio.run(go())
    _check(store)
    calls = [c for c in llm.calls if "Your task: Comparables" in call_text(c)]
    names = {t["name"] for t in calls[0]["tools"]}
    assert set(B.NAMES) <= names  # the browser agent has them ...
    other = next(c for c in llm.calls if "Your task: Summarize" in call_text(c))
    assert not any(t["name"].startswith("browser_") for t in other["tools"])  # ... nobody else does
    assert "a dedicated browser already signed into 127.0.0.1" in call_text(calls[0])
    # the browser budget (15) replaced ATLAS_MAX_TURNS (4): 6 tool turns fit
    assert "desarrollo,precio_m2" in json.dumps(calls[-1]["messages"], ensure_ascii=False)


def test_browser_tools_in_a_live_mission_subscription(site, browser_env):
    human_login("oracle-test", site)
    reg = browser_registry(site)
    sdk = FakeClaudeSDK()
    sdk.when(lambda s: "STAGE: PLANNING" in s.prompt, [call("create_plan", {"tasks": PLAN}), say("ok")])
    sdk.when(lambda s: "Your task: Comparables" in s.prompt,
             [*[call(n, a) for n, a in STEPS], call("submit_report", REPORT), say("done")])
    sdk.when(lambda s: "Your task: Summarize" in s.prompt, [call("submit_report", SUMMARY), say("done")])
    sdk.when(lambda s: "STAGE: REVIEW" in s.prompt, [call("request_followups", {"tasks": []}), say("ok")])
    sdk.when(lambda s: "STAGE: CONSOLIDATION" in s.prompt, [call("submit_mission_report", FINAL), say("ok")])

    async def go():
        store = WorldStore(reg, EventBus())
        engine = LiveEngine(store, loader=AgentLoader(reg, MODELS),
                            context=NodeContext(reg, local_dir=Path("/nonexistent")),
                            config=LiveConfig(models=MODELS, max_turns=4), prices=PriceTable(),
                            backend="subscription", sdk_query=sdk)
        m = await engine.start("Comparables San Pedro", "corporate")
        await asyncio.wait_for(engine.wait(m.id), 60)
        return store

    store = asyncio.run(go())
    _check(store)
    sess = next(s for s in sdk.sessions if "Your task: Comparables" in s.prompt)
    assert "mcp__atlas__browser_download" in sess.options.allowed_tools
    assert sess.options.max_turns == 15
    assert "no-chrome" in sess.options.extra_args  # still never the user's Chrome
    assert "dedicated browser" in sess.system
    other = next(s for s in sdk.sessions if "Your task: Summarize" in s.prompt)
    assert not any("browser_" in t for t in other.options.allowed_tools)


def test_configured_but_unusable_browser_is_explained(monkeypatch):
    from atlas.live.runtime import AgentRun

    reg = AgentRegistry.load()
    reg._agents["oracle"] = reg.get("oracle").model_copy(
        update={"browser": BrowserConfig(profile="x", allowed_domains=["${UNSET_DOMAINS}"])})
    monkeypatch.delenv("UNSET_DOMAINS", raising=False)

    class Scope:  # the bits AgentRun.__init__ and the note need
        def __init__(self):
            self.agents = {"oracle": AgentLoader(reg, MODELS).resolve("oracle")}
            self.config = LiveConfig(models=MODELS, max_turns=5)
            self.node, self.mission_id = "corporate", "msn_x"

    class T:
        assigned_to = "oracle"

    run = AgentRun(Scope(), T(), [])  # type: ignore[arg-type]
    assert not run.has_browser and run.max_turns == 5
    assert not any(t["name"].startswith("browser_") for t in run._tools())
    assert "configured but unavailable" in run._files_note() and "allowed_domains" in run._files_note()


def test_session_export_import_moves_the_login_to_another_profile(site, browser_env, tmp_path, monkeypatch):
    """Windows -> Linux server: a Chrome profile can't be copied, the session (cookies + localStorage) can."""
    from types import SimpleNamespace

    from playwright.sync_api import sync_playwright

    human_login("pc", site)
    with sync_playwright() as pw:  # a cookie of another site in the same profile must not travel
        ctx = B.launch_context(pw, "pc", show=False)
        ctx.add_cookies([{"name": "other", "value": "x", "domain": "example.org", "path": "/"}])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{site}/estudio")
        page.evaluate("localStorage.setItem('filtro', 'San Pedro')")
        ctx.close()
    agent = SimpleNamespace(id="market-studies", name="MERCATO", browser=BrowserConfig(
        profile="pc", start_url=f"{site}/estudio", allowed_domains=["127.0.0.1"]))
    monkeypatch.setattr(B, "_find", lambda _id: agent)
    out = tmp_path / "session.json"
    assert B.export_session("mercato", str(out)) == 0
    state = __import__("json").loads(out.read_text())
    assert [c["name"] for c in state["cookies"]] == ["sid"] and state["origins"][0]["localStorage"]
    if os.name != "nt":
        assert (out.stat().st_mode & 0o777) == 0o600

    agent.browser = BrowserConfig(profile="server", start_url=f"{site}/estudio", allowed_domains=["127.0.0.1"])
    assert B.import_session("mercato", str(out)) == 0
    with sync_playwright() as pw:
        ctx = B.launch_context(pw, "server", show=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{site}/estudio")
        assert "Estudio de mercado" in page.content()
        assert page.evaluate("localStorage.getItem('filtro')") == "San Pedro"
        ctx.close()
    assert B._cookie_for(".4srealestate.com", ["redi.4srealestate.com"])
    assert not B._cookie_for("evil.com", ["redi.4srealestate.com"])
