"""Agent registry: loads every YAML file in /agents into AgentDefinition objects.

Adding, removing or swapping an agent never touches code — drop a file in
/agents (or set `enabled: false`). External agents are registered the same
way, with `kind: external` and an adapter (http | mcp | cli).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import AdapterType, AgentDefinition, AgentKind

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parents[4] / "agents"


class RegistryError(ValueError):
    pass


class AgentRegistry:
    def __init__(self, agents: dict[str, AgentDefinition]):
        self._agents = agents

    @classmethod
    def load(cls, directory: Path | str = DEFAULT_AGENTS_DIR) -> AgentRegistry:
        directory = Path(directory)
        agents: dict[str, AgentDefinition] = {}
        for path in sorted(directory.glob("*.y*ml")):
            if path.name.startswith("_"):
                continue  # _template.yaml etc.
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            agent = AgentDefinition.model_validate(data)
            if agent.id in agents:
                raise RegistryError(f"duplicate agent id '{agent.id}' in {path.name}")
            _validate(agent, path.name)
            agents[agent.id] = agent
        orchestrators = [a for a in agents.values() if a.is_orchestrator and a.enabled]
        if len(orchestrators) != 1:
            raise RegistryError(f"exactly one enabled orchestrator required, found {len(orchestrators)}")
        return cls(agents)

    def all(self, include_disabled: bool = False) -> list[AgentDefinition]:
        agents = [a for a in self._agents.values() if include_disabled or a.enabled]
        return sorted(agents, key=lambda a: not a.is_orchestrator)  # orchestrator first, then file order

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise RegistryError(f"unknown agent '{agent_id}'") from exc

    @property
    def orchestrator(self) -> AgentDefinition:
        return next(a for a in self._agents.values() if a.is_orchestrator and a.enabled)

    def specialists(self) -> list[AgentDefinition]:
        return [a for a in self.all() if not a.is_orchestrator]


def _validate(agent: AgentDefinition, filename: str) -> None:
    if agent.kind == AgentKind.EXTERNAL and agent.adapter == AdapterType.CLAUDE:
        raise RegistryError(f"{filename}: external agents need an http, mcp or cli adapter")
    required = {AdapterType.HTTP: "url", AdapterType.CLI: "command", AdapterType.MCP: "server"}
    key = required.get(AdapterType(agent.adapter))
    if key and key not in agent.adapter_config:
        raise RegistryError(f"{filename}: adapter '{agent.adapter}' requires adapter_config.{key}")
