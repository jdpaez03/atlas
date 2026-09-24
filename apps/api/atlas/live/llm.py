"""LLM access for the live engine.

`LLMClient` is anything with `async create(**kwargs)` taking the Messages API arguments and
returning a Message-like object (`.content` blocks with `.type`, `.stop_reason`, `.usage`).
`AnthropicLLM` wraps `AsyncAnthropic`; `FakeLLM` returns scripted responses for tests.
`Meter` performs every call on behalf of a mission and records its usage in the store.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from ..core.models import Usage
from ..core.store import WorldStore
from .pricing import PriceTable

log = logging.getLogger("atlas.live")


class LLMError(RuntimeError):
    """The model could not be reached (after the retry) or answered unusably."""


class UsageLimitError(LLMError):
    """The plan's usage limit (or a hard rate limit) was hit: retrying now won't help."""


class LLMClient(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicLLM:
    """AsyncAnthropic with one retry (exponential backoff) on transient API errors."""

    def __init__(self, api_key: str | None = None, *, retries: int = 1, backoff: float = 2.0,
                 timeout: float = 600.0, http_client: Any = None):
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY"), max_retries=0, timeout=timeout,
            http_client=http_client,
        )
        self.retries = retries
        self.backoff = backoff

    @staticmethod
    def _transient(exc: Exception) -> bool:
        import anthropic

        if isinstance(exc, anthropic.APIConnectionError):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code in (408, 409, 429) or exc.status_code >= 500
        return False

    async def create(self, **kwargs: Any) -> Any:
        import anthropic

        attempt = 0
        while True:
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.APIError as exc:
                if attempt >= self.retries or not self._transient(exc):
                    raise LLMError(f"{type(exc).__name__}: {exc}") from exc
                delay = self.backoff * (2**attempt)
                log.warning("LLM call failed (%s), retrying in %.1fs", exc, delay)
                attempt += 1
                await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Message-like objects (used by FakeLLM, and as the shape the runtime relies on)
# ---------------------------------------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass
class FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class FakeMessage:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
    model: str = "fake"


def text(value: str, **usage: int) -> FakeMessage:
    """A reply with one text block that ends the turn."""
    return FakeMessage([FakeBlock("text", text=value)], "end_turn", FakeUsage(**usage))


def tool_use(name: str, input: dict[str, Any], *, say: str | None = None, **usage: int) -> FakeMessage:
    """A reply that calls one tool (optionally preceded by some text)."""
    blocks = [FakeBlock("text", text=say)] if say else []
    blocks.append(FakeBlock("tool_use", id=f"toolu_{uuid4().hex[:10]}", name=name, input=input))
    return FakeMessage(blocks, "tool_use", FakeUsage(**usage))


Response = FakeMessage | Exception | Callable[[dict[str, Any]], Any]
Matcher = Callable[[dict[str, Any]], bool]


class FakeLLM:
    """Scripted LLM for tests (no network).

    Responses come from, in order of precedence:
      1. rules added with `when(matcher, *responses)`: the first rule whose matcher accepts the
         call kwargs and still has responses left serves the next one (`repeat=True` keeps
         serving the last response forever);
      2. the positional `script`, consumed in call order.
    A response may be a Message-like object, an Exception (raised), or a callable
    `(kwargs) -> response`. Every call's kwargs are recorded in `calls`.
    """

    def __init__(self, script: list[Response] | None = None):
        self.script: list[Response] = list(script or [])
        self.rules: list[tuple[Matcher, list[Response], bool]] = []
        self.calls: list[dict[str, Any]] = []

    def when(self, matcher: Matcher, *responses: Response, repeat: bool = False) -> FakeLLM:
        self.rules.append((matcher, list(responses), repeat))
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(0)  # behave like I/O so concurrent tasks interleave
        response: Response | None = None
        for matcher, responses, repeat in self.rules:
            if responses and matcher(kwargs):
                response = responses[0] if (repeat and len(responses) == 1) else responses.pop(0)
                break
        if response is None:
            if not self.script:
                raise LLMError("FakeLLM: no scripted response for this call")
            response = self.script.pop(0)
        if callable(response) and not isinstance(response, FakeMessage):
            response = response(kwargs)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def block_to_param(block: Any) -> dict[str, Any]:
    """Turn a response content block into a request param (to echo assistant turns back)."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text or ""}
    if btype == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input or {}}
    if hasattr(block, "model_dump"):  # server_tool_use, web_search_tool_result, thinking...
        return block.model_dump(mode="json", exclude_none=True)
    if isinstance(block, dict):
        return block
    raise LLMError(f"cannot echo content block of type {btype!r}")


def response_text(message: Any) -> str:
    return "\n".join(b.text for b in message.content if getattr(b, "type", None) == "text" and b.text).strip()


def tool_uses(message: Any) -> list[Any]:
    return [b for b in message.content if getattr(b, "type", None) == "tool_use"]


def call_text(kwargs: dict[str, Any]) -> str:
    """Everything a call sends as text (system + messages): handy for FakeLLM matchers."""
    parts: list[str] = []
    system = kwargs.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif system:
        parts.extend(b.get("text", "") for b in system)
    for m in kwargs.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                c = b.get("content")
                parts.append(c if isinstance(c, str) else str(c))
    return "\n".join(parts)


class Meter:
    """Performs LLM calls for one mission and records usage + estimated cost in the store."""

    def __init__(self, llm: LLMClient | None, store: WorldStore, mission_id: str, prices: PriceTable):
        self.llm = llm  # None on the subscription backend (no Messages API calls; only record_totals)
        self.store = store
        self.mission_id = mission_id
        self.prices = prices

    async def create(self, **kwargs: Any) -> Any:
        if self.llm is None:
            raise LLMError("no Messages API client on this backend")
        try:
            message = await self.llm.create(**kwargs)
        except LLMError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # any client failure is an LLM failure for the engine
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        await self.record(kwargs.get("model", ""), getattr(message, "usage", None))
        return message

    async def record(self, model: str, usage: Any) -> None:
        if usage is None:
            return
        fresh = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cost = self.prices.estimate(
            model, input_tokens=fresh, output_tokens=out, cache_read_tokens=read, cache_write_tokens=write
        )
        delta = Usage(
            input_tokens=fresh + write,  # cache writes are input tokens billed at a premium
            output_tokens=out,
            cache_read_tokens=read,
            llm_calls=1,
            est_cost_usd=cost,
        )
        await self.store.update_usage(self.mission_id, delta)

    async def record_totals(self, model: str, *, input_tokens: int = 0, output_tokens: int = 0,
                            cache_read_tokens: int = 0, cache_write_tokens: int = 0, calls: int = 1,
                            cost_usd: float | None = None) -> None:
        """Record a whole session's usage (subscription backend). `cost_usd` (e.g. the SDK's API-equivalent
        total_cost_usd) wins over the price-table estimate."""
        cost = cost_usd if cost_usd is not None else self.prices.estimate(
            model, input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
        )
        delta = Usage(
            input_tokens=input_tokens + cache_write_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            llm_calls=max(1, calls),
            est_cost_usd=float(cost),
        )
        await self.store.update_usage(self.mission_id, delta)
