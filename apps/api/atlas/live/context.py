"""Private node context from `ATLAS_LOCAL_DIR` (never committed).

    <local>/context/<node>/**/*.md|txt              every agent working a mission of <node>
    <local>/context/<node>/<division>/**/*.md|txt   only that division's members, and ATLAS

Isolation: only the mission's own node folder is ever read. Paths that resolve outside it (e.g. a
symlink to another node's folder) are skipped. The result is truncated to `max_chars` per agent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.models import AgentDefinition
from ..core.registry import AgentRegistry

log = logging.getLogger("atlas.live")

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LOCAL_DIR = REPO_ROOT.parent / "atlas-local"
DEFAULT_MAX_CHARS = 150_000
SUFFIXES = {".md", ".txt"}
TRUNCATED = "\n\n[... context truncated ...]"


def default_local_dir() -> Path:
    """`ATLAS_LOCAL_DIR` (relative paths are relative to the repo root), else ../atlas-local."""
    raw = os.getenv("ATLAS_LOCAL_DIR")
    if not raw:
        return DEFAULT_LOCAL_DIR
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def default_max_chars() -> int:
    try:
        return max(1000, int(os.getenv("ATLAS_CONTEXT_MAX_CHARS", DEFAULT_MAX_CHARS)))
    except ValueError:
        return DEFAULT_MAX_CHARS


class NodeContext:
    def __init__(self, registry: AgentRegistry, local_dir: Path | str | None = None,
                 max_chars: int | None = None):
        self.registry = registry
        self.local_dir = Path(local_dir) if local_dir is not None else default_local_dir()
        self.max_chars = max_chars or default_max_chars()

    @property
    def root(self) -> Path:
        return self.local_dir / "context"

    def _node_dir(self, node: str) -> Path | None:
        known = {n.id for n in self.registry.nodes()} | {d.node for d in self.registry.divisions()}
        if node not in known:  # node ids are validated slugs, but never trust a path segment
            return None
        d = self.root / node
        return d if d.is_dir() else None

    def files(self, node: str, agent: AgentDefinition | None) -> list[Path]:
        """Context files visible to `agent` (None or the orchestrator = everything in the node)."""
        node_dir = self._node_dir(node)
        if node_dir is None:
            return []
        base = node_dir.resolve()
        divisions = {d.id for d in self.registry.divisions() if d.node == node}
        sees_all = agent is None or agent.is_orchestrator
        out: list[Path] = []
        for path in sorted(node_dir.rglob("*")):
            if path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            try:
                real = path.resolve()
                real.relative_to(base)  # symlinks must not escape the node folder
            except (OSError, ValueError):
                continue
            rel = path.relative_to(node_dir)
            top = rel.parts[0] if len(rel.parts) > 1 else None
            if top in divisions and not sees_all and (agent is None or agent.division != top):
                continue
            out.append(path)
        return out

    def load(self, node: str, agent: AgentDefinition | None = None) -> str:
        node_dir = self._node_dir(node)
        if node_dir is None:
            return ""
        parts: list[str] = []
        for path in self.files(node, agent):
            try:
                body = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                log.warning("cannot read context file %s: %s", path, exc)
                continue
            if body:
                parts.append(f"### {path.relative_to(node_dir).as_posix()}\n{body}")
        text = "\n\n".join(parts)
        if len(text) > self.max_chars:
            text = text[: self.max_chars - len(TRUNCATED)] + TRUNCATED
        return text

    def nodes_with_context(self) -> list[str]:
        return [n.id for n in self.registry.nodes() if self.files(n.id, None)]
