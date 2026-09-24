"""Agent registry.

Layout of /agents:
    organization.yaml            nodes (corporate, personal, ...) and divisions (eos, ...)
    *.yaml                       core agents (shared across nodes unless `nodes:` says otherwise)
    <node>/<division>/*.yaml     node-bound specialists (any subfolder depth works)
    _*.yaml                      templates, ignored

Adding, removing or swapping an agent never touches code. External agents use
`kind: external` and an adapter (http | mcp | cli | claude_md).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .models import AdapterType, AgentDefinition, AgentKind, DivisionDefinition, NodeDefinition

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parents[4] / "agents"
ORG_FILE = "organization.yaml"
SHARED = "*"


class RegistryError(ValueError):
    pass


class AgentRegistry:
    def __init__(
        self,
        agents: dict[str, AgentDefinition],
        nodes: dict[str, NodeDefinition],
        divisions: dict[str, DivisionDefinition],
    ):
        self._agents = agents
        self._nodes = nodes
        self._divisions = divisions

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, directory: Path | str = DEFAULT_AGENTS_DIR) -> AgentRegistry:
        directory = Path(directory)
        nodes, divisions = _load_org(directory / ORG_FILE)

        agents: dict[str, AgentDefinition] = {}
        for path in sorted(directory.rglob("*.y*ml")):
            if path.name.startswith("_") or path.name == ORG_FILE:
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            agent = AgentDefinition.model_validate(data)
            rel = path.relative_to(directory).as_posix()
            if agent.id in agents:
                raise RegistryError(f"duplicate agent id '{agent.id}' in {rel}")
            _validate(agent, rel, nodes, divisions)
            agents[agent.id] = agent

        orchestrators = [a for a in agents.values() if a.is_orchestrator and a.enabled]
        if len(orchestrators) != 1:
            raise RegistryError(f"exactly one enabled orchestrator required, found {len(orchestrators)}")
        for div in divisions.values():
            if div.lead and div.lead not in agents:
                raise RegistryError(f"division '{div.id}' lead '{div.lead}' is not a registered agent")
        return cls(agents, nodes, divisions)

    # -- queries -------------------------------------------------------------

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

    def nodes(self) -> list[NodeDefinition]:
        return [n for n in self._nodes.values() if n.enabled]

    def divisions(self) -> list[DivisionDefinition]:
        return list(self._divisions.values())

    def division_members(self, division_id: str) -> list[AgentDefinition]:
        return [a for a in self.all() if a.division == division_id]

    def agents_for_node(self, node_id: str) -> list[AgentDefinition]:
        """Agents allowed to work on a mission in `node_id` (isolation rule)."""
        if node_id not in self._nodes:
            raise RegistryError(f"unknown node '{node_id}'")
        return [a for a in self.all() if SHARED in a.nodes or node_id in a.nodes]

    def can_work_in(self, agent_id: str, node_id: str) -> bool:
        agent = self.get(agent_id)
        return SHARED in agent.nodes or node_id in agent.nodes


# -- helpers -----------------------------------------------------------------


def _load_org(path: Path) -> tuple[dict[str, NodeDefinition], dict[str, DivisionDefinition]]:
    if not path.exists():
        raise RegistryError(f"missing {path.name}: define at least one node")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes = {n.id: n for n in (NodeDefinition.model_validate(x) for x in data.get("nodes", []))}
    if not nodes:
        raise RegistryError(f"{path.name}: define at least one node")
    divisions: dict[str, DivisionDefinition] = {}
    for raw in data.get("divisions", []):
        div = DivisionDefinition.model_validate(raw)
        if div.node not in nodes:
            raise RegistryError(f"division '{div.id}' references unknown node '{div.node}'")
        divisions[div.id] = div
    return nodes, divisions


def _validate(
    agent: AgentDefinition,
    rel: str,
    nodes: dict[str, NodeDefinition],
    divisions: dict[str, DivisionDefinition],
) -> None:
    adapter = AdapterType(agent.adapter)
    if agent.kind == AgentKind.EXTERNAL and adapter == AdapterType.CLAUDE:
        raise RegistryError(f"{rel}: external agents need an http, mcp, cli or claude_md adapter")
    required = {
        AdapterType.HTTP: "url",
        AdapterType.CLI: "command",
        AdapterType.MCP: "server",
        AdapterType.CLAUDE_MD: "path",
    }
    key = required.get(adapter)
    if key and key not in agent.adapter_config:
        raise RegistryError(f"{rel}: adapter '{agent.adapter}' requires adapter_config.{key}")

    for node in agent.nodes:
        if node != SHARED and node not in nodes:
            raise RegistryError(f"{rel}: unknown node '{node}'")
    if SHARED in agent.nodes and len(agent.nodes) > 1:
        raise RegistryError(f"{rel}: use either nodes: ['*'] or a list of node ids, not both")
    if agent.division:
        div = divisions.get(agent.division)
        if not div:
            raise RegistryError(f"{rel}: unknown division '{agent.division}'")
        if agent.nodes != [div.node]:
            raise RegistryError(f"{rel}: division '{div.id}' members must have nodes: ['{div.node}']")


_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def resolve_path(value: str) -> Path:
    """Expand ${ENV_VAR} and ~ in adapter paths, so personal paths stay out of the public repo."""
    return Path(os.path.expanduser(_ENV.sub(lambda m: os.getenv(m.group(1), m.group(0)), value)))
