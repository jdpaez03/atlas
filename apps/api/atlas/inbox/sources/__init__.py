"""Mail sources (docs/INBOX.md §1): where the inbox engine reads mail from.

ATLAS_MAIL_SOURCE = graph | folder | auto (default auto: graph if ATLAS_MS_CLIENT_ID is set, else folder if
ATLAS_MAIL_FOLDER is set, else no source).
"""

from __future__ import annotations

import os

from .base import MailMessage, MailSource, MailSourceError, SourceStatus

__all__ = ["MailMessage", "MailSource", "MailSourceError", "SourceStatus", "make_source", "reset_source"]

_cached: tuple[tuple, MailSource | None] | None = None


def _config() -> tuple:
    keys = ("ATLAS_MAIL_SOURCE", "ATLAS_MS_CLIENT_ID", "ATLAS_MS_TENANT_ID", "ATLAS_MS_DRAFTS", "ATLAS_MAIL_FOLDER",
            "ATLAS_MAIL_ME", "ATLAS_LOCAL_DIR")  # fmt: skip
    return tuple(os.getenv(k, "") for k in keys)


def make_source(*, fresh: bool = False) -> MailSource | None:
    """The configured mail source, or None when none is configured.

    The instance is reused while the configuration is unchanged, so a device-code sign-in started through one call
    (POST /inbox/connect) is visible to the next (GET /inbox/connect/status). `fresh=True` builds a new one.
    Raises ValueError for an unknown ATLAS_MAIL_SOURCE.
    """
    global _cached
    config = _config()
    if not fresh and _cached is not None and _cached[0] == config:
        return _cached[1]
    kind = (os.getenv("ATLAS_MAIL_SOURCE") or "auto").strip().lower()
    client_id = os.getenv("ATLAS_MS_CLIENT_ID", "").strip()
    folder = os.getenv("ATLAS_MAIL_FOLDER", "").strip()
    if kind == "auto":
        kind = "graph" if client_id else ("folder" if folder else "none")
    source: MailSource | None
    if kind == "graph":
        from .graph import GraphSource

        source = GraphSource()
    elif kind == "folder":
        from .folder import FolderSource

        source = FolderSource()
    elif kind in ("none", "off", ""):
        source = None
    else:
        raise ValueError(f"ATLAS_MAIL_SOURCE must be graph, folder or auto (got {kind!r})")
    _cached = (config, source)
    return source


def reset_source() -> None:
    global _cached
    _cached = None
