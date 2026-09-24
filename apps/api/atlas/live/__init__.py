"""Phase 2: live missions on Claude — the API or a Claude Max plan via Claude Code (docs/LIVE.md).

    backend.py       ATLAS_LLM_BACKEND (api | subscription | auto): detection and live hints
    executor.py      what differs per backend (structured ATLAS step, agent task); ApiExecutor
    sdk.py           SubscriptionExecutor on the Claude Agent SDK, FakeClaudeSDK (tests)
    llm.py           LLMClient protocol, AnthropicLLM, FakeLLM (tests), Meter (usage → store)
    pricing.py       token price table (estimates), ATLAS_PRICES override
    agent_loader.py  role prompt + model per agent (YAML / claude_md), availability
    context.py       private node context from ATLAS_LOCAL_DIR (node-isolated)
    prompts.py       ATLAS protocol, orchestrator prompts, tool schemas
    files.py         file sandbox (ATLAS_FILE_ROOTS_<NODE>), read/list/search tools, extraction, deliverables
    evidence.py      system-recorded evidence, claim check, mission deliverables
    runtime.py       one agent run (tool-use loop), LiveConfig, MissionScope
    orchestrator.py  LiveMission (plan → execute → review → consolidate) and LiveEngine
"""

from .agent_loader import AgentLoader, ModelConfig, ResolvedAgent
from .backend import BackendInfo, detect_backend
from .context import NodeContext
from .llm import AnthropicLLM, FakeLLM, LLMClient, LLMError, UsageLimitError
from .orchestrator import LiveEngine, LiveMission, validate_plan
from .pricing import PriceTable
from .runtime import LiveConfig

__all__ = [
    "AgentLoader",
    "AnthropicLLM",
    "BackendInfo",
    "FakeLLM",
    "LLMClient",
    "LLMError",
    "LiveConfig",
    "LiveEngine",
    "LiveMission",
    "ModelConfig",
    "NodeContext",
    "PriceTable",
    "ResolvedAgent",
    "UsageLimitError",
    "detect_backend",
    "validate_plan",
]
