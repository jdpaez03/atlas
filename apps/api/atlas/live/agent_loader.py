"""Resolve each agent's role prompt and model for live missions.

    adapter claude     -> the YAML `system_prompt` (fallback: a generic prompt built from
                          title / description / capabilities)
    adapter claude_md  -> the body of the Claude Code agent file at `adapter_config.path`
                          (`${ENV}` and `~` expanded). Frontmatter `model: opus|sonnet|haiku|inherit`
                          maps to the configured models. A missing/unreadable file makes the agent
                          UNAVAILABLE: it is never planned, but GET /agents still lists it.
    http / cli / mcp   -> unavailable in live mode (Phase 5)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import yaml

from ..core.models import AdapterType, AgentDefinition
from ..core.registry import AgentRegistry, resolve_path

log = logging.getLogger("atlas.live")

DEFAULT_ORCHESTRATOR_MODEL = "claude-opus-5-5"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_FAST_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class ModelConfig:
    orchestrator: str = DEFAULT_ORCHESTRATOR_MODEL
    default: str = DEFAULT_MODEL
    fast: str = DEFAULT_FAST_MODEL

    @classmethod
    def from_env(cls) -> ModelConfig:
        return cls(
            orchestrator=os.getenv("ATLAS_ORCHESTRATOR_MODEL") or DEFAULT_ORCHESTRATOR_MODEL,
            default=os.getenv("ATLAS_MODEL") or DEFAULT_MODEL,
            fast=os.getenv("ATLAS_FAST_MODEL") or DEFAULT_FAST_MODEL,
        )

    def resolve(self, alias: str | None, *, fallback: str | None = None) -> str:
        """Map `opus`/`sonnet`/`haiku`/`inherit` (Claude Code aliases) or a full model id."""
        fallback = fallback or self.default
        if not alias:
            return fallback
        key = alias.strip().lower()
        return {
            "inherit": fallback,
            "opus": self.orchestrator,
            "sonnet": self.default,
            "haiku": self.fast,
        }.get(key, alias.strip())


@dataclass(frozen=True)
class ResolvedAgent:
    agent: AgentDefinition
    available: bool
    role_prompt: str = ""
    model: str = DEFAULT_MODEL
    source: str = "yaml"  # yaml | generic | claude_md | none
    reason: str | None = None

    @property
    def id(self) -> str:
        return self.agent.id


def generic_prompt(agent: AgentDefinition) -> str:
    caps = ", ".join(c.replace("_", " ") for c in agent.capabilities) or "general analysis"
    return (
        f"You are {agent.name}, the {agent.title} agent of ATLAS.\n"
        f"Responsibility: {agent.description}\n"
        f"Capabilities: {caps}.\n"
        "Work rigorously within your specialty, be concise and concrete, and hand back findings "
        "that other agents and the human can act on."
    )


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a Claude Code agent file into (frontmatter dict, body)."""
    text = raw.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), "\n".join(lines[i + 1 :]).strip()
    return {}, text.strip()


class AgentLoader:
    def __init__(self, registry: AgentRegistry, models: ModelConfig | None = None):
        self.registry = registry
        self.models = models or ModelConfig()
        self._logged: set[tuple[str, str]] = set()

    def resolve(self, agent_id: str) -> ResolvedAgent:
        agent = self.registry.get(agent_id)
        fallback = self.models.orchestrator if agent.is_orchestrator else self.models.default
        model = self.models.resolve(agent.model, fallback=fallback)
        if not agent.enabled:
            return ResolvedAgent(agent, False, model=model, source="none", reason="agent is disabled")
        adapter = AdapterType(agent.adapter)
        if adapter in (AdapterType.CLAUDE, AdapterType.MOCK):
            if agent.system_prompt and agent.system_prompt.strip():
                return ResolvedAgent(agent, True, agent.system_prompt.strip(), model, "yaml")
            return ResolvedAgent(agent, True, generic_prompt(agent), model, "generic")
        if adapter == AdapterType.CLAUDE_MD:
            return self._claude_md(agent, fallback)
        return self._unavailable(agent, model, f"adapter '{adapter.value}' is not supported in live mode yet")

    def _claude_md(self, agent: AgentDefinition, fallback: str) -> ResolvedAgent:
        raw_path = str(agent.adapter_config.get("path", ""))
        path = resolve_path(raw_path)
        model = self.models.resolve(agent.model, fallback=fallback)
        if "${" in str(path):
            return self._unavailable(agent, model, f"unresolved variable in path '{raw_path}' (set it in .env)")
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return self._unavailable(agent, model, f"cannot read {path}: {exc.strerror or exc}")
        if not body:
            return self._unavailable(agent, model, f"{path} has no prompt body")
        model = self.models.resolve(agent.model or meta.get("model"), fallback=fallback)
        return ResolvedAgent(agent, True, body, model, "claude_md")

    def _unavailable(self, agent: AgentDefinition, model: str, reason: str) -> ResolvedAgent:
        if (agent.id, reason) not in self._logged:
            self._logged.add((agent.id, reason))
            log.warning("agent %s unavailable for live missions: %s", agent.id, reason)
        return ResolvedAgent(agent, False, model=model, source="none", reason=reason)

    # -- queries -------------------------------------------------------------

    def availability(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for agent in self.registry.all(include_disabled=True):
            r = self.resolve(agent.id)
            out[agent.id] = {"available": r.available} | ({"reason": r.reason} if r.reason else {})
        return out

    def roster(self, node: str) -> list[ResolvedAgent]:
        """Specialists that may be planned for a mission in `node` (allowed there AND available)."""
        out = []
        for agent in self.registry.agents_for_node(node):
            if agent.is_orchestrator:
                continue
            r = self.resolve(agent.id)
            if r.available:
                out.append(r)
        return out
