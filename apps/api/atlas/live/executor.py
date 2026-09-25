"""What differs between LLM backends, behind one small interface.

The orchestrator's flow (plan → DAG scheduling → follow-ups → consolidation, phases, statuses, cancel)
is shared. A backend only has to:

  structured(...)  run one structured ATLAS step (plan / follow-ups / mission report): make the model call
                   exactly one tool and return that tool's input (optionally validated, with retries);
  run_task(...)    run one agent task (tool loop with consult, request_approval, submit_report and the
                   optional web tools) and return its AgentReport.

`ApiExecutor` is the Messages API implementation (forced tool_choice, our own tool loop in runtime.py).
`SubscriptionExecutor` (sdk.py) drives Claude Code through the Claude Agent SDK.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..core.models import AgentReport, Task
from .llm import block_to_param, tool_uses
from .prompts import plan_errors_message, system_blocks
from .runtime import AgentRun, MissionScope

# validate(tool_input or None when the tool wasn't called) -> errors (empty = valid)
Validator = Callable[[dict[str, Any] | None], Awaitable[list[str]]]


class Executor(Protocol):
    backend: str

    async def structured(self, scope: MissionScope, *, model: str, system: list[str], prompt: str,
                         tool: dict[str, Any], max_tokens: int, validate: Validator | None = None,
                         attempts: int = 1) -> dict[str, Any] | None: ...

    async def run_task(self, scope: MissionScope, task: Task, dep_reports: list[str]) -> AgentReport: ...


def invalid_input_message(tool_name: str, errors: list[str]) -> str:
    return "The input is invalid:\n- " + "\n- ".join(errors) + f"\nFix these problems and call {tool_name} again."


class ApiExecutor:
    """Claude Messages API (ANTHROPIC_API_KEY), through the mission's Meter."""

    backend = "api"

    async def structured(self, scope: MissionScope, *, model: str, system: list[str], prompt: str,
                         tool: dict[str, Any], max_tokens: int, validate: Validator | None = None,
                         attempts: int = 1) -> dict[str, Any] | None:
        """Force `tool`; return its input. With `validate`, invalid input goes back to the model as an error
        tool_result and it tries again, up to `attempts` calls. Raises LLMError when the call fails."""
        name = tool["name"]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        for attempt in range(max(1, attempts)):
            resp = await scope.meter.create(
                model=model, max_tokens=max_tokens, system=system_blocks(*system), tools=[tool],
                tool_choice={"type": "tool", "name": name}, messages=messages,
            )
            uses = tool_uses(resp)
            use = next((u for u in uses if u.name == name), None)
            data = (use.input or {}) if use is not None else None
            if validate is None:
                return data
            errors = await validate(data)
            if not errors:
                return data
            if attempt + 1 >= attempts:
                break
            content = [block_to_param(b) for b in resp.content]
            if content:
                messages.append({"role": "assistant", "content": content})
            feedback = plan_errors_message(errors) if name == "create_plan" else invalid_input_message(name, errors)
            results: list[dict[str, Any]] = [
                {"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                 "content": feedback if u is use else "Ignored."}
                for u in uses
            ]
            if not results:
                results = [{"type": "text", "text": feedback}]
            messages.append({"role": "user", "content": results})
        return None

    async def run_task(self, scope: MissionScope, task: Task, dep_reports: list[str]) -> AgentReport:
        run = AgentRun(scope, task, dep_reports)
        try:
            return await run.run()
        finally:
            await run.close()
