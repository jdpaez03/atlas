"""Adapt ATLAS's .env to the Linux server (called by install-server.sh and import-from-pc.sh).

    python3 server_env.py <.env> --web https://atlas.tail1234.ts.net --api https://atlas.tail1234.ts.net:8443

- sets ATLAS_CORS_ORIGINS / NEXT_PUBLIC_ATLAS_API_URL to the tailnet URLs;
- ATLAS_FILE_ROOTS_* pointing at Windows folders: the OneDrive folder becomes `onedrive:/` (read through
  Microsoft Graph; there is no OneDrive client on Linux), other Windows folders are commented out;
- any other value that is a Windows path (C:/..., C:\\...) is commented out, with its key listed;
- nothing else changes, and values are never printed (the file holds secrets).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

WIN_PATH = re.compile(r"^[A-Za-z]:[\\/]")
DROP_WHEN_WINDOWS = {"ATLAS_LOCAL_DIR", "ATLAS_BROWSER_EXECUTABLE", "ATLAS_CLAUDE_CLI"}
KEY = re.compile(r"^\s*([A-Z0-9_]+)\s*=(.*)$")


def _parts(value: str) -> list[str]:
    return [p.strip().strip('"') for p in value.strip().strip('"').split(";") if p.strip()]


def adapt(lines: list[str], web: str, api: str) -> tuple[list[str], list[str]]:
    out: list[str] = []
    notes: list[str] = []
    wanted = {"ATLAS_CORS_ORIGINS": web, "NEXT_PUBLIC_ATLAS_API_URL": api}
    seen: set[str] = set()
    for line in lines:
        m = KEY.match(line)
        if not m:
            out.append(line)
            continue
        key, value = m.group(1), m.group(2).strip()
        if key in wanted:
            if key not in seen:
                out.append(f"{key}={wanted[key]}")
            seen.add(key)
            continue
        parts = _parts(value)
        windows = [p for p in parts if WIN_PATH.match(p)]
        if key.startswith("ATLAS_FILE_ROOTS_") and windows:
            keep = [p for p in parts if not WIN_PATH.match(p)]
            lost = windows
            if key == "ATLAS_FILE_ROOTS_CORPORATE" and any("onedrive" in p.lower() for p in windows):
                keep = ["onedrive:/", *[p for p in keep if p.lower() != "onedrive:/"]]
                lost = [p for p in windows if "onedrive" not in p.lower()]
                notes.append(f"{key}: the OneDrive folder is now onedrive:/ (Microsoft Graph)")
            if lost:
                notes.append(f"{key}: {len(lost)} Windows folder(s) left out")
            out.append(f"# (Windows) {line.strip()}")
            out.append(f"{key}={';'.join(keep)}")
            continue
        if windows or (key in DROP_WHEN_WINDOWS and WIN_PATH.match(value.strip('"'))):
            out.append(f"# (Windows, not on this server) {line.strip()}")
            notes.append(f"{key}: Windows path commented out")
            continue
        out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return out, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("env")
    ap.add_argument("--web", required=True)
    ap.add_argument("--api", required=True)
    args = ap.parse_args(argv)
    path = Path(args.env)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    out, notes = adapt(lines, args.web, args.api)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for n in notes:
        print(" -", n)
    print(f" - web {args.web} · API {args.api}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
