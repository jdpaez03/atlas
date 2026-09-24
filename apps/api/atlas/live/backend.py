"""Which LLM backend runs live missions (docs/LIVE.md § Backends).

    ATLAS_LLM_BACKEND=api           Claude API with ANTHROPIC_API_KEY (billed per token)
    ATLAS_LLM_BACKEND=subscription  Claude Agent SDK → Claude Code CLI, logged in with your Pro/Max plan
    ATLAS_LLM_BACKEND=auto          (default) api if ANTHROPIC_API_KEY is set, else subscription if the
                                    Claude Code CLI is usable, else live missions are unavailable

`detect_backend()` never starts anything: it only checks the key, the SDK import and where the CLI is.
Whether the CLI is logged in can only be known when a session starts (a login error fails that task
with a clear message).
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

BACKENDS = ("api", "subscription")
LOGIN_HINT = "install Claude Code and log in (run `claude`, then /login with your Pro/Max account)"


@dataclass(frozen=True)
class BackendInfo:
    backend: str | None  # "api" | "subscription" | None (live unavailable)
    hint: str | None = None  # why live is unavailable (None when available)

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def cost_basis(self) -> str | None:
        """`api`: estimated API bill. `api_equivalent`: what the tokens would cost on the API (plan usage)."""
        return {"api": "api", "subscription": "api_equivalent"}.get(self.backend or "")


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def sdk_installed() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _windows() -> bool:
    return platform.system() == "Windows"


def find_claude_cli() -> str | None:
    """Where the SDK will find Claude Code: ATLAS_CLAUDE_CLI, the SDK's bundled CLI, or PATH.

    Mirrors claude_agent_sdk's own discovery (SubprocessCLITransport._find_cli). On Windows only a
    native claude.exe counts: the SDK refuses npm's claude.cmd shim.
    """
    explicit = os.getenv("ATLAS_CLAUDE_CLI", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.is_file() else None
    exe = "claude.exe" if _windows() else "claude"
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / exe
        if bundled.is_file():
            return str(bundled)
    except ImportError:
        return None
    if _windows():
        for hit in (shutil.which("claude.exe"), shutil.which("claude")):
            if hit and hit.lower().endswith((".exe", ".com")):
                return hit
        local = Path.home() / ".local" / "bin" / "claude.exe"
        return str(local) if local.is_file() else None
    if hit := shutil.which("claude"):
        return hit
    for path in (
        Path.home() / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        Path.home() / ".local/bin/claude",
        Path.home() / "node_modules/.bin/claude",
        Path.home() / ".yarn/bin/claude",
        Path.home() / ".claude/local/claude",
    ):
        if path.is_file():
            return str(path)
    return None


def loop_can_spawn() -> bool:
    """False on Windows when the running loop is a SelectorEventLoop (no subprocess support).

    uvicorn picks a SelectorEventLoop on Windows whenever it runs with --reload or --workers
    (uvicorn/loops/asyncio.py: Proactor only when `use_subprocess` is False). `python -m atlas`
    always uses a Proactor loop.
    """
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return True  # not inside a loop: nothing to check
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is not None and isinstance(loop, proactor)


def subscription_problem() -> str | None:
    """Why the subscription backend can't run here, or None if it can."""
    if not sdk_installed():
        return "claude-agent-sdk is not installed — run `uv sync` in apps/api"
    if find_claude_cli() is None:
        if os.getenv("ATLAS_CLAUDE_CLI", "").strip():
            return f"ATLAS_CLAUDE_CLI does not point to a file — fix it or {LOGIN_HINT}"
        return f"Claude Code CLI not found — {LOGIN_HINT}"
    if not loop_can_spawn():
        return ("The API runs on a Windows selector event loop (uvicorn --reload), which cannot start "
                "Claude Code — start it with `uv run python -m atlas`")
    return None


def detect_backend(override: str | None = None) -> BackendInfo:
    choice = (override or os.getenv("ATLAS_LLM_BACKEND") or "auto").strip().lower()
    if choice == "api":
        if has_api_key():
            return BackendInfo("api")
        return BackendInfo(None, "Add ANTHROPIC_API_KEY to .env (or set ATLAS_LLM_BACKEND=subscription to use "
                                 "your Claude Max plan)")
    if choice == "subscription":
        problem = subscription_problem()
        return BackendInfo(None, problem) if problem else BackendInfo("subscription")
    if choice != "auto":
        return BackendInfo(None, f"Unknown ATLAS_LLM_BACKEND '{choice}' (use api, subscription or auto)")
    if has_api_key():
        return BackendInfo("api")
    problem = subscription_problem()
    if problem is None:
        return BackendInfo("subscription")
    return BackendInfo(None, f"Add ANTHROPIC_API_KEY to .env, or use your Claude Max plan: {problem}")
