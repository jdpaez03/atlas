"""OneDrive / SharePoint read roots through Microsoft Graph (docs/SERVER_LINUX.md § Files), against a fake Graph."""

from __future__ import annotations

import io
from urllib.parse import unquote

import httpx
import pytest

from atlas.live import graphfiles as gf
from atlas.live.files import FileSandbox, FileTools
from atlas.live.graphfiles import GraphFiles, GraphFilesError, RemotePath

MID = "msn_graph0001"


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Precios"
    ws.append(["Proyecto", "MXN/m2"])
    ws.append(["Balcones 800", 72000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeGraph:
    """Two drives: the user's OneDrive (d1) and a SharePoint library (d2). Paths are case-insensitive."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.throttle_once = False
        self._add("d1", "root", "root", None, folder=True)
        self._add("d1", "f1", "PAGA", "root", folder=True)
        self._add("d1", "f2", "Estudios", "f1", folder=True)
        self._add("d1", "x1", "Balcones.xlsx", "f2", data=_xlsx())
        self._add("d1", "t1", "notas.txt", "f2", data="Absorción 2.1 unidades/mes".encode())
        self._add("d1", "e1", ".env", "f1", data=b"SECRET=1")
        self._add("d1", "f3", "Personal", "f1", folder=True)
        self._add("d1", "p1", "diario notas.txt", "f3", data=b"PERSONAL")
        self._add("d1", "f4", "Otro", "root", folder=True)
        self._add("d1", "s1", "secreto notas.txt", "f4", data=b"SECRET")
        self._add("d1", "old", "viejo.xls", "f2", data=b"\xd0\xcf")
        self._add("d2", "root2", "root", None, folder=True)
        self._add("d2", "r1", "Reportes", "root2", folder=True)
        self._add("d2", "q3", "Q3.txt", "r1", data=b"Ventas Q3: 41 unidades")

    def _add(self, drive: str, iid: str, name: str, parent: str | None, *, folder: bool = False,
             data: bytes = b"") -> None:
        self.items[iid] = {"id": iid, "name": name, "drive": drive, "parent": parent, "folder": folder}
        if not folder:
            self.content[iid] = data

    def _path(self, iid: str) -> list[str]:
        out: list[str] = []
        it = self.items[iid]
        while it["parent"] is not None:
            out.insert(0, it["name"])
            it = self.items[it["parent"]]
        return out

    def _json(self, iid: str, *, with_path: bool = True) -> dict:
        it = self.items[iid]
        out: dict = {"id": iid, "name": it["name"], "eTag": f'"{iid}-1"', "lastModifiedDateTime": "2026-09-20T10:00:00Z",
                     "webUrl": f"https://paga-my.sharepoint.com/{iid}"}
        if it["folder"]:
            out["folder"] = {"childCount": 1}
        else:
            out["file"] = {}
            out["size"] = len(self.content[iid])
        if it["parent"] is None:
            out["root"] = {}
        else:
            ref: dict = {"driveId": it["drive"], "id": it["parent"]}
            if with_path:
                ref["path"] = f"/drives/{it['drive']}/root:" + "".join(
                    "/" + p for p in self._path(it["parent"])).replace(" ", "%20")
            out["parentReference"] = ref
        return out

    def _descendants(self, iid: str) -> list[str]:
        out = []
        for cid, c in self.items.items():
            if c["parent"] == iid:
                out.append(cid)
                out += self._descendants(cid)
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.calls.append(path)
        assert request.headers.get("authorization") == "Bearer tok" or request.url.host == "download.example"
        if self.throttle_once:
            self.throttle_once = False
            return httpx.Response(429, headers={"Retry-After": "1"})
        if request.url.host == "download.example":
            return httpx.Response(200, content=self.content[path.strip("/")])
        p = path.removeprefix("/v1.0")
        if p == "/me/drive":
            return httpx.Response(200, json={"id": "d1"})
        if p == "/sites/paga.sharepoint.com:/sites/Finanzas":
            return httpx.Response(200, json={"id": "site1"})
        if p.startswith("/sites/"):
            if p == "/sites/site1/drives":
                return httpx.Response(200, json={"value": [
                    {"id": "dx", "name": "Otra", "webUrl": "https://paga.sharepoint.com/sites/Finanzas/Otra"},
                    {"id": "d2", "name": "Documents",
                     "webUrl": "https://paga.sharepoint.com/sites/Finanzas/Documentos%20compartidos"}]})
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "no site"}})
        _, _, drive, rest = p.split("/", 3)
        if rest == "root":
            return httpx.Response(200, json=self._json("root" if drive == "d1" else "root2"))
        if rest.startswith("root:/"):
            want = [x.casefold() for x in rest[len("root:/"):].split("/")]
            for iid, it in self.items.items():
                if it["drive"] == drive and [x.casefold() for x in self._path(iid)] == want:
                    return httpx.Response(200, json=self._json(iid))
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "not found"}})
        parts = rest.split("/")  # items/<id>[/children|/content|/search(...)]
        iid = parts[1]
        if len(parts) == 2:
            return httpx.Response(200, json=self._json(iid))
        if parts[2] == "children":
            kids = [cid for cid, c in self.items.items() if c["parent"] == iid]
            return httpx.Response(200, json={"value": [self._json(c) for c in kids]})
        if parts[2] == "content":
            return httpx.Response(302, headers={"Location": f"https://download.example/{iid}"})
        if parts[2].startswith("search(q='"):
            q = parts[2][len("search(q='"):-2].replace("''", "'").casefold()
            hits = []
            for cid in self._descendants(iid):
                c = self.items[cid]
                blob = (c["name"] + " " + self.content.get(cid, b"").decode("utf-8", "ignore")).casefold()
                if all(w in blob for w in q.split()):
                    hits.append(self._json(cid, with_path=cid != "t1"))  # t1 comes without its folder path
            return httpx.Response(200, json={"value": hits})
        return httpx.Response(400, json={"error": {"code": "bad", "message": path}})


@pytest.fixture
def graph(monkeypatch, tmp_path):
    fake = FakeGraph()
    monkeypatch.setenv("ATLAS_FILE_ROOTS_CORPORATE",
                       "onedrive:/PAGA;sharepoint:paga.sharepoint.com/sites/Finanzas/Documentos compartidos")
    monkeypatch.setenv("ATLAS_FILE_ROOTS_PERSONAL", "onedrive:/PAGA/Personal")
    client = GraphFiles(lambda: "tok", transport=httpx.MockTransport(fake.handler), cache_dir=tmp_path / "cache",
                        sleep=lambda s: None)
    return fake, FileTools(FileSandbox.for_mission("corporate", MID), graph=client)


def test_parse_labels_and_roots(caplog):
    p = gf.parse("onedrive:\\PAGA\\Estudios/")
    assert p.parts == ("PAGA", "Estudios") and p.label == "onedrive:/PAGA/Estudios"
    sp = gf.parse("sharepoint:PAGA.sharepoint.com/Sites/Finanzas/Documentos compartidos/Q3")
    assert sp.drive.site == "sites/Finanzas" and sp.drive.library == "Documentos compartidos"
    assert sp.label == "sharepoint:paga.sharepoint.com/sites/Finanzas/Documentos compartidos/Q3"
    for bad in ("onedrive:/a/../b", "sharepoint:host/x/y/z", "sharepoint:host/sites/x"):
        with pytest.raises(GraphFilesError):
            gf.parse(bad)
    local, remote = gf.split_roots("C:/Users/x/Docs; onedrive:/ ;sharepoint:bad")
    assert local == ["C:/Users/x/Docs"] and [r.label for r in remote] == ["onedrive:/"]
    assert "ignoring file root" in caplog.text
    assert gf.rel_from_parent("/drives/d/root:/A%20B/c", "x.pdf") == ("A B", "c", "x.pdf")
    assert gf.glob_words("*balcones*.xlsx") == "balcones xlsx"


def test_sandbox_rules_for_remote_paths(graph):
    _, tools = graph
    sb = tools.sb
    assert sb.user_roots == [] and sb.readable_labels == [
        "onedrive:/PAGA", "sharepoint:paga.sharepoint.com/sites/Finanzas/Documentos compartidos"]
    assert sb.check_remote(gf.parse("onedrive:/paga/ESTUDIOS/Balcones.xlsx")) is None  # case-insensitive
    assert "another node" in sb.check_remote(gf.parse("onedrive:/PAGA/Personal/diario notas.txt"))
    assert "outside" in sb.check_remote(gf.parse("onedrive:/Otro/secreto notas.txt"))
    assert "denied" in sb.check_remote(gf.parse("onedrive:/PAGA/.env"))
    personal = FileSandbox.for_mission("personal", MID)
    assert personal.check_remote(gf.parse("onedrive:/PAGA/Personal/diario notas.txt")) is None
    assert "outside" in personal.check_remote(gf.parse("onedrive:/PAGA/Estudios/notas.txt"))


def test_list_read_and_cache(graph):
    fake, tools = graph
    roots = tools.run("list_files", {})
    assert "onedrive:/PAGA  (OneDrive/SharePoint" in roots.text
    listing = tools.run("list_files", {"path": "onedrive:/PAGA", "recursive": True})
    assert listing.ok and listing.ref == "onedrive:/PAGA"
    assert "Estudios/Balcones.xlsx" in listing.text and "Estudios/notas.txt" in listing.text
    assert "Personal" not in listing.text and ".env" not in listing.text
    only = tools.run("list_files", {"path": "onedrive:/PAGA/Estudios", "pattern": "*.xlsx"})
    assert "Balcones.xlsx" in only.text and "notas.txt" not in only.text

    res = tools.run("read_file", {"path": "onedrive:/PAGA/Estudios/Balcones.xlsx"})
    assert res.ok and res.kind == "file_read" and res.ref == "onedrive:/PAGA/Estudios/Balcones.xlsx"
    assert "Balcones 800 | 72000" in res.text and "## Sheet: Precios" in res.text
    assert res.extra["web_url"].endswith("/x1")
    downloads = sum(1 for c in fake.calls if c.endswith("/content"))
    tools.run("read_file", {"path": "onedrive:/PAGA/Estudios/Balcones.xlsx", "offset": 10})
    assert sum(1 for c in fake.calls if c.endswith("/content")) == downloads == 1  # cached by eTag

    rel = tools.run("read_file", {"path": "Estudios/notas.txt"})  # relative: tried against the remote roots
    assert rel.ok and rel.ref == "onedrive:/PAGA/Estudios/notas.txt" and "Absorción" in rel.text

    for path, why in (("onedrive:/PAGA/Personal/diario notas.txt", "another node"),
                      ("onedrive:/Otro/secreto notas.txt", "outside"), ("onedrive:/PAGA/.env", "denied"),
                      ("onedrive:/PAGA/nope.txt", "does not exist"), ("onedrive:/PAGA/Estudios", "is a folder"),
                      ("onedrive:/PAGA/Estudios/viejo.xls", ".xls workbooks are not supported")):
        bad = tools.run("read_file", {"path": path})
        assert not bad.ok and why in bad.text, (path, bad.text)
    assert not any("/items/p1" in c or "/items/s1" in c for c in fake.calls)


def test_search_uses_microsoft_search_and_filters(graph):
    _, tools = graph
    res = tools.run("search_files", {"query": "notas"})
    lines = res.text.splitlines()
    assert any(line.startswith("onedrive:/PAGA/Estudios/notas.txt") for line in lines)  # path looked up
    assert not any("Personal" in line or "Otro" in line for line in lines)
    content = tools.run("search_files", {"query": "absorción", "under": "onedrive:/PAGA/Estudios"})
    assert "names and contents" in content.text and "onedrive:/PAGA/Estudios/notas.txt" in content.text
    glob = tools.run("search_files", {"query": "*.xlsx"})
    assert "onedrive:/PAGA/Estudios/Balcones.xlsx" in glob.text and "notas" not in glob.text
    sp = tools.run("search_files", {"query": "ventas"})
    assert "sharepoint:paga.sharepoint.com/sites/Finanzas/Documentos compartidos/Reportes/Q3.txt" in sp.text


def test_sharepoint_library_by_url_name_and_throttling(graph):
    fake, tools = graph
    fake.throttle_once = True
    res = tools.run("read_file", {
        "path": "sharepoint:paga.sharepoint.com/sites/Finanzas/Documentos compartidos/Reportes/Q3.txt"})
    assert res.ok and "41 unidades" in res.text
    bad = tools.run("list_files", {"path": "sharepoint:paga.sharepoint.com/sites/Nada/Docs"})
    assert not bad.ok and "outside" in bad.text


def test_not_signed_in_is_a_clear_tool_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_FILE_ROOTS_CORPORATE", "onedrive:/PAGA")
    monkeypatch.delenv("ATLAS_MS_CLIENT_ID", raising=False)
    monkeypatch.setattr(gf, "_APP", None)
    tools = FileTools(FileSandbox.for_mission("corporate", MID),
                      graph=GraphFiles(transport=httpx.MockTransport(FakeGraph().handler), cache_dir=tmp_path))
    res = tools.run("read_file", {"path": "onedrive:/PAGA/Estudios/notas.txt"})
    assert not res.ok and "ATLAS_MS_CLIENT_ID" in res.text
    search = tools.run("search_files", {"query": "notas"})  # an unreachable remote root is a note, not a failure
    assert search.ok and "[onedrive:/PAGA not searched:" in search.text


def test_outlook_sign_in_also_asks_for_files_when_needed(monkeypatch):
    from atlas.inbox.sources.graph import GraphSource

    src = GraphSource("cid", "tenant", drafts=False, app=object())
    monkeypatch.delenv("ATLAS_FILE_ROOTS_CORPORATE", raising=False)
    monkeypatch.setenv("ATLAS_FILE_ROOTS_PERSONAL", "")
    assert src._login_scopes() == ["Mail.Read"]
    monkeypatch.setenv("ATLAS_FILE_ROOTS_CORPORATE", "C:/x;onedrive:/PAGA")
    assert src._login_scopes() == ["Mail.Read", "Files.Read.All", "Sites.Read.All"]
    assert RemotePath(gf.Drive("onedrive"), ("A",)).within(gf.parse("onedrive:/"))
