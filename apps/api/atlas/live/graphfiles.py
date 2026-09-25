"""OneDrive / SharePoint read roots through Microsoft Graph (docs/SERVER_LINUX.md § Files).

On a Windows PC the OneDrive client syncs the work files to a local folder and the agents read that folder. On
the Linux server there is no OneDrive client, so a node's read roots can also be remote:

    ATLAS_FILE_ROOTS_CORPORATE=onedrive:/PAGA;sharepoint:contoso.sharepoint.com/sites/Finanzas/Documentos compartidos

    onedrive:/<path>                                         the signed-in user's OneDrive ("/" = all of it)
    sharepoint:<host>/sites/<site>/<library>[/<path>]        a document library of a SharePoint site
                                                              (teams/<site> works too; the library by its name
                                                              or the last segment of its URL)

Agents address remote files by those labels ("onedrive:/PAGA/Estudios/Balcones.xlsx"); a relative path that
doesn't exist locally is tried against the remote roots. The same sandbox rules apply: the path must sit under
one of this node's roots, the deepest matching root must be this node's, and protected names are denied.

Auth: the Entra app of the Outlook connection (ATLAS_MS_CLIENT_ID) with delegated Files.Read.All and
Sites.Read.All, read-only. Sign in once with `atlas-graph login` (device code: works on a headless server) or
Connect in Follow-ups; the token cache is shared (<ATLAS_LOCAL_DIR>/inbox/msal_cache.bin).

Reading downloads the file into <ATLAS_LOCAL_DIR>/graphcache/ (keyed by item id + eTag, so an edited file is
fetched again) and extracts its text like a local file. Nothing is ever written to OneDrive / SharePoint.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

from ..core import paths

log = logging.getLogger("atlas.graphfiles")

GRAPH = "https://graph.microsoft.com/v1.0"
FILE_SCOPES = ["Files.Read.All", "Sites.Read.All"]
SCHEMES = ("onedrive:", "sharepoint:")
MAX_RETRIES = 4
MAX_RETRY_WAIT = 30.0
MAX_BYTES = 25 * 1024 * 1024
MAX_REQUESTS = 250  # Graph calls per recursive listing
MAX_PATH_LOOKUPS = 40  # search hits whose folder has to be looked up
SELECT = "id,name,size,lastModifiedDateTime,folder,file,remoteItem,parentReference,eTag,webUrl,root"
_CACHE_KEEP = 400  # downloaded files kept in the cache


class GraphFilesError(Exception):
    """A remote file operation failed (the message is safe to show the agent)."""


def is_remote(text: str | None) -> bool:
    return (text or "").strip().strip('"').strip("'").lower().startswith(SCHEMES)


def _parts(raw: str) -> list[str]:
    out = [p for p in raw.replace("\\", "/").split("/") if p.strip()]
    if any(p.strip() in (".", "..") for p in out):
        raise GraphFilesError("'.' and '..' are not allowed in remote paths")
    return [p.strip() for p in out]


def _fold(parts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(p.casefold() for p in parts)


# ---------------------------------------------------------------------------
# Roots and paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drive:
    """Where a remote path lives: the user's OneDrive or one SharePoint library."""

    kind: str  # onedrive | sharepoint
    host: str = ""
    site: str = ""  # "sites/Finanzas" / "teams/Obra"
    library: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        return (self.kind, self.host.casefold(), self.site.casefold(), self.library.casefold())

    def label(self, parts: tuple[str, ...] | list[str] = ()) -> str:
        if self.kind == "onedrive":
            return "onedrive:/" + "/".join(parts)
        return "sharepoint:" + "/".join([self.host, self.site, self.library, *parts])


@dataclass(frozen=True)
class RemotePath:
    drive: Drive
    parts: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.drive.label(self.parts)

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else self.label

    def child(self, name: str) -> RemotePath:
        return RemotePath(self.drive, (*self.parts, name))

    def within(self, root: RemotePath) -> bool:
        n = len(root.parts)
        return self.drive.key == root.drive.key and _fold(self.parts[:n]) == _fold(root.parts)


def parse(raw: str) -> RemotePath:
    """'onedrive:/a/b' or 'sharepoint:host/sites/x/Library/a/b' -> RemotePath. Raises GraphFilesError."""
    text = (raw or "").strip().strip('"').strip("'")
    low = text.lower()
    if low.startswith("onedrive:"):
        return RemotePath(Drive("onedrive"), tuple(_parts(text[len("onedrive:"):])))
    if low.startswith("sharepoint:"):
        parts = _parts(text[len("sharepoint:"):])
        if len(parts) < 4 or parts[1].lower() not in ("sites", "teams"):
            raise GraphFilesError(f"'{raw}': use sharepoint:<host>/sites/<site>/<library>/<path>")
        drive = Drive("sharepoint", parts[0].lower(), f"{parts[1].lower()}/{parts[2]}", parts[3])
        return RemotePath(drive, tuple(parts[4:]))
    raise GraphFilesError(f"'{raw}' is not a onedrive: or sharepoint: path")


def split_roots(raw: str) -> tuple[list[str], list[RemotePath]]:
    """ATLAS_FILE_ROOTS_<NODE> value -> (local entries, remote roots). Invalid remote entries are logged."""
    local: list[str] = []
    remote: list[RemotePath] = []
    for part in (raw or "").split(";"):
        part = part.strip().strip('"')
        if not part:
            continue
        if is_remote(part):
            try:
                remote.append(parse(part))
            except GraphFilesError as exc:
                log.warning("ignoring file root %s", exc)
        else:
            local.append(part)
    return local, remote


def remote_roots_configured() -> bool:
    return any(is_remote(p) for k, v in os.environ.items() if k.startswith("ATLAS_FILE_ROOTS_")
               for p in v.split(";"))


# ---------------------------------------------------------------------------
# Token (shared MSAL app + cache with the Outlook connection)
# ---------------------------------------------------------------------------

_APP: Any = None
_APP_LOCK = threading.Lock()


def msal_app() -> Any:
    global _APP
    with _APP_LOCK:
        if _APP is None:
            from ..inbox.sources.graph import build_msal_app

            client_id = os.getenv("ATLAS_MS_CLIENT_ID", "").strip()
            if not client_id:
                raise GraphFilesError("OneDrive/SharePoint roots need the Microsoft 365 app: set ATLAS_MS_CLIENT_ID "
                                      "(docs/INBOX_SETUP.md A) and sign in with `atlas-graph login`")
            _APP = build_msal_app(client_id, os.getenv("ATLAS_MS_TENANT_ID", "").strip() or "organizations")
        return _APP


def _account(app: Any) -> dict | None:
    accounts = app.get_accounts()
    want = os.getenv("ATLAS_MS_ACCOUNT", "").strip().lower()
    for acc in accounts:
        if want and (acc.get("username") or "").lower() == want:
            return acc
    return accounts[0] if accounts else None


def graph_token() -> str:
    app = msal_app()
    account = _account(app)
    result = app.acquire_token_silent(FILE_SCOPES, account=account) if account else None
    if not result or "access_token" not in result:
        raise GraphFilesError("not signed in to Microsoft 365 for files (or Files.Read.All was not granted): "
                              "the human must run `atlas-graph login` on the ATLAS machine")
    return result["access_token"]


# ---------------------------------------------------------------------------
# Graph client (synchronous: file tools run in a worker thread)
# ---------------------------------------------------------------------------


@dataclass
class Item:
    drive_id: str
    id: str
    name: str
    is_dir: bool
    size: int = 0
    modified: str = ""
    etag: str = ""
    web_url: str = ""
    parent_path: str | None = None  # "/drives/<id>/root:/a/b" when Graph gives it
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, data: dict[str, Any], drive_id: str) -> Item:
        remote = data.get("remoteItem") or {}
        target_drive = ((remote.get("parentReference") or {}).get("driveId")) if remote else None
        is_dir = bool(data.get("folder") or (remote and remote.get("folder")))
        return cls(
            drive_id=target_drive or (data.get("parentReference") or {}).get("driveId") or drive_id,
            id=str(remote.get("id") or data.get("id") or ""), name=str(data.get("name") or ""),
            is_dir=is_dir, size=int(data.get("size") or remote.get("size") or 0),
            modified=str(data.get("lastModifiedDateTime") or ""), etag=str(data.get("eTag") or remote.get("eTag") or ""),
            web_url=str(data.get("webUrl") or ""), parent_path=(data.get("parentReference") or {}).get("path"),
            raw=data,
        )

    @property
    def mtime(self) -> str:
        if not self.modified:
            return ""
        try:
            return datetime.fromisoformat(self.modified).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.modified[:16]


class GraphFiles:
    """Read-only OneDrive / SharePoint access. One instance per mission's file tools (drive ids cached)."""

    def __init__(self, token: Callable[[], str] = graph_token, *, transport: httpx.BaseTransport | None = None,
                 cache_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 60.0):
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._timeout = httpx.Timeout(timeout, connect=10.0)
        self._cache_dir = cache_dir
        self._drives: dict[tuple[str, ...], str] = {}
        self._client: httpx.Client | None = None
        self.requests = 0

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir or paths.local_dir() / "graphcache"

    # -- HTTP --

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(transport=self._transport, timeout=self._timeout, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        if not url.startswith("http"):
            url = GRAPH + url
        attempt = 0
        while True:
            self.requests += 1
            try:
                resp = self._http().get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            except httpx.TimeoutException as exc:
                if attempt >= MAX_RETRIES:
                    raise GraphFilesError(f"Microsoft Graph timed out: {exc}") from exc
                attempt += 1
                self._sleep(min(2.0 ** attempt, MAX_RETRY_WAIT))
                continue
            except httpx.HTTPError as exc:
                raise GraphFilesError(f"Microsoft Graph unreachable: {exc}") from exc
            if resp.status_code in (429, 503, 504) and attempt < MAX_RETRIES:
                attempt += 1
                try:
                    wait = float(resp.headers.get("Retry-After") or 2.0 ** attempt)
                except ValueError:
                    wait = 2.0 ** attempt
                self._sleep(max(0.0, min(wait, MAX_RETRY_WAIT)))
                continue
            return resp

    def _json(self, url: str, params: dict[str, str] | None = None, *, missing_ok: bool = False) -> dict | None:
        resp = self.get(url, params)
        if resp.status_code == 404 and missing_ok:
            return None
        if resp.status_code >= 400:
            raise GraphFilesError(_error_text(resp))
        return resp.json()

    # -- drives and items --

    def drive_id(self, drive: Drive) -> str:
        if drive.key in self._drives:
            return self._drives[drive.key]
        if drive.kind == "onedrive":
            data = self._json("/me/drive", {"$select": "id"})
            did = str((data or {}).get("id") or "")
        else:
            site = self._json(f"/sites/{drive.host}:/{quote(drive.site)}", {"$select": "id"}, missing_ok=True)
            if not site:
                raise GraphFilesError(f"SharePoint site {drive.host}/{drive.site} not found (or no access)")
            drives = self._json(f"/sites/{site['id']}/drives", {"$select": "id,name,webUrl"}) or {}
            want = drive.library.casefold()
            did = ""
            for d in drives.get("value", []):
                last = unquote(str(d.get("webUrl") or "").rstrip("/").rsplit("/", 1)[-1]).casefold()
                if str(d.get("name") or "").casefold() == want or last == want:
                    did = str(d["id"])
                    break
            if not did:
                names = ", ".join(str(d.get("name")) for d in drives.get("value", []))
                raise GraphFilesError(f"no library '{drive.library}' in {drive.host}/{drive.site} (libraries: {names})")
        if not did:
            raise GraphFilesError("could not open the drive")
        self._drives[drive.key] = did
        return did

    def item(self, path: RemotePath) -> Item | None:
        """The item at `path` (None when it doesn't exist). Falls back to walking folder by folder, which also
        follows 'Add shortcut to My files' links that path addressing can't cross."""
        did = self.drive_id(path.drive)
        if not path.parts:
            data = self._json(f"/drives/{did}/root", {"$select": SELECT})
            return Item.of(data or {}, did)
        rel = quote("/".join(path.parts))
        data = self._json(f"/drives/{did}/root:/{rel}", {"$select": SELECT}, missing_ok=True)
        if data:
            return Item.of(data, did)
        current = Item.of(self._json(f"/drives/{did}/root", {"$select": SELECT}) or {}, did)
        for name in path.parts:
            if not current.is_dir:
                return None
            nxt = next((c for c in self.children(current) if c.name.casefold() == name.casefold()), None)
            if nxt is None:
                return None
            current = nxt
        return current

    def children(self, folder: Item, limit: int = 2000) -> list[Item]:
        out: list[Item] = []
        url: str | None = f"/drives/{folder.drive_id}/items/{folder.id}/children"
        params: dict[str, str] | None = {"$select": SELECT, "$top": "200"}
        while url and len(out) < limit:
            data = self._json(url, params) or {}
            params = None
            out += [Item.of(x, folder.drive_id) for x in data.get("value", [])]
            url = data.get("@odata.nextLink")
        return out[:limit]

    def search(self, folder: Item, query: str, limit: int = 100) -> list[Item]:
        q = query.replace("'", "''")
        url: str | None = f"/drives/{folder.drive_id}/items/{folder.id}/search(q='{quote(q)}')"
        params: dict[str, str] | None = {"$select": SELECT, "$top": "100"}
        out: list[Item] = []
        while url and len(out) < limit:
            data = self._json(url, params) or {}
            params = None
            out += [Item.of(x, folder.drive_id) for x in data.get("value", [])]
            url = data.get("@odata.nextLink")
        return out[:limit]

    def parent_path(self, item: Item) -> str | None:
        """'/drives/<id>/root:/a/b' of an item's folder (search results often omit it)."""
        if item.parent_path:
            return item.parent_path
        data = self._json(f"/drives/{item.drive_id}/items/{item.id}", {"$select": "id,parentReference"},
                          missing_ok=True)
        return ((data or {}).get("parentReference") or {}).get("path")

    def download(self, item: Item) -> Path:
        """The item's bytes in the local cache (re-downloaded when its eTag changes)."""
        if item.size > MAX_BYTES:
            raise GraphFilesError(f"'{item.name}' is larger than {MAX_BYTES // (1024 * 1024)} MB")
        tag = hashlib.sha1(f"{item.drive_id}:{item.id}:{item.etag}".encode()).hexdigest()[:16]
        ext = Path(item.name).suffix.lower()[:10]
        folder = self.cache_dir
        target = folder / f"{tag}{ext}"
        if target.is_file():
            os.utime(target)
            return target
        resp = self.get(f"/drives/{item.drive_id}/items/{item.id}/content")
        if resp.status_code >= 400:
            raise GraphFilesError(_error_text(resp))
        if len(resp.content) > MAX_BYTES:
            raise GraphFilesError(f"'{item.name}' is larger than {MAX_BYTES // (1024 * 1024)} MB")
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(folder, 0o700)
        tmp = folder / f".{tag}.part"
        tmp.write_bytes(resp.content)
        os.replace(tmp, target)
        _prune(folder)
        return target


def _prune(folder: Path) -> None:
    try:
        files = sorted((p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for p in files[_CACHE_KEEP:]:
        try:
            p.unlink()
        except OSError:
            pass


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        text = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    except Exception:  # noqa: BLE001 — not JSON
        text = resp.text[:200]
    if resp.status_code == 401:
        return f"Microsoft 365 sign-in expired ({text}): the human must run `atlas-graph login`"
    if resp.status_code == 403:
        return f"access denied by Microsoft 365 ({text})"
    if resp.status_code == 404:
        return "not found"
    return f"Graph {resp.status_code}: {text}"


def rel_from_parent(parent_path: str | None, name: str) -> tuple[str, ...] | None:
    """'/drives/x/root:/a/b' + 'c.pdf' -> ('a', 'b', 'c.pdf'); None when the path isn't in that form."""
    if not parent_path or "root:" not in parent_path:
        return None
    rest = unquote(parent_path.split("root:", 1)[1])
    return (*[p for p in rest.split("/") if p], name)


def glob_words(query: str) -> str:
    """The words Graph can search for in a glob ('*balcones*.xlsx' -> 'balcones xlsx')."""
    return " ".join(w for w in "".join(c if c not in "*?[]" else " " for c in query).replace(".", " ").split())


def name_matches(name: str, query: str) -> bool:
    q = query.strip().lower()
    return fnmatch.fnmatch(name.lower(), q) if any(c in q for c in "*?[") else True


# ---------------------------------------------------------------------------
# CLI: atlas-graph login | status | roots | ls <path>
# ---------------------------------------------------------------------------


def _login(files_only: bool) -> int:
    from ..inbox.sources.graph import DRAFT_SCOPES, READ_SCOPES

    app = msal_app()
    scopes = list(FILE_SCOPES)
    if not files_only:
        scopes += READ_SCOPES
        if os.getenv("ATLAS_MS_DRAFTS", "1").strip().lower() not in ("0", "false", "no", "off"):
            scopes += DRAFT_SCOPES
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        print("Could not start the sign-in:", flow.get("error_description") or flow)
        return 1
    print(flow.get("message") or f"Open {flow.get('verification_uri')} and enter {flow.get('user_code')}")
    print("(from any device: your phone or your PC; waiting…)")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print("Sign-in failed:", result.get("error_description") or result.get("error"))
        return 1
    who = (result.get("id_token_claims") or {}).get("preferred_username", "?")
    print(f"Signed in as {who}. Granted: {result.get('scope')}")
    return 0


def _status() -> int:
    app = msal_app()
    acc = _account(app)
    if not acc:
        print("Not signed in. Run: atlas-graph login")
        return 1
    ok = app.acquire_token_silent(FILE_SCOPES, account=acc)
    print(f"Account: {acc.get('username')}")
    print("Files: " + ("OK" if ok and "access_token" in ok else "no token (run: atlas-graph login)"))
    return 0 if ok and "access_token" in ok else 1


def _roots() -> int:
    g = GraphFiles()
    bad = 0
    for key, value in sorted(os.environ.items()):
        if not key.startswith("ATLAS_FILE_ROOTS_"):
            continue
        for root in split_roots(value)[1]:
            try:
                it = g.item(root)
                state = "OK (folder)" if it and it.is_dir else "not a folder" if it else "NOT FOUND"
            except GraphFilesError as exc:
                state = f"ERROR {exc}"
            bad += not state.startswith("OK")
            print(f"{key[len('ATLAS_FILE_ROOTS_'):].lower():<12} {root.label}  ->  {state}")
    return 1 if bad else 0


def _ls(raw: str) -> int:
    g = GraphFiles()
    it = g.item(parse(raw))
    if it is None:
        print("not found")
        return 1
    for c in g.children(it) if it.is_dir else [it]:
        print(f"{c.name}{'/' if c.is_dir else ''}\t{'' if c.is_dir else c.size}\t{c.mtime}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    try:
        from dotenv import load_dotenv

        load_dotenv(paths.REPO_ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass
    ap = argparse.ArgumentParser(prog="atlas-graph", description="Microsoft 365 sign-in and OneDrive/SharePoint "
                                                                 "roots for ATLAS")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="sign in with a device code (mail + files)")
    lg.add_argument("--files-only", action="store_true", help="only Files.Read.All / Sites.Read.All")
    sub.add_parser("status", help="who is signed in and whether files can be read")
    sub.add_parser("roots", help="check every onedrive:/sharepoint: root in ATLAS_FILE_ROOTS_*")
    ls = sub.add_parser("ls", help="list a remote folder, e.g. onedrive:/ or onedrive:/PAGA")
    ls.add_argument("path")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "login":
            return _login(args.files_only)
        if args.cmd == "status":
            return _status()
        if args.cmd == "roots":
            return _roots()
        return _ls(args.path)
    except GraphFilesError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
