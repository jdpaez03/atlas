"""Where ATLAS keeps private, per-machine data (never committed).

    <ATLAS_LOCAL_DIR>/                    default: <repo>/../atlas-local
      context/<node>/...                  node context (read by agents, node-isolated)
      agents/*.md                         private agent prompts (claude_md adapter)
      atlas.db                            mission history (SQLite event log)
      missions/<mission_id>/attachments/  files the user attached to a mission
      outputs/<node>/<mission_id>/        deliverables agents wrote
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LOCAL_DIR = REPO_ROOT.parent / "atlas-local"
_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def local_dir() -> Path:
    raw = os.getenv("ATLAS_LOCAL_DIR")
    if not raw:
        return DEFAULT_LOCAL_DIR
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def db_path() -> Path:
    return local_dir() / "atlas.db"


def attachments_dir(mission_id: str) -> Path:
    return local_dir() / "missions" / safe_name(mission_id) / "attachments"


def outputs_dir(node: str, mission_id: str) -> Path:
    return local_dir() / "outputs" / safe_name(node) / safe_name(mission_id)


def safe_name(name: str) -> str:
    """A filename with no path separators or odd characters (keeps accents out for Windows safety)."""
    cleaned = _SAFE.sub("_", Path(name).name).strip(" .")
    return cleaned[:150] or "file"


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
