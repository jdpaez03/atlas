"""Subscription backend: live missions on a Claude Pro/Max plan via the Claude Agent SDK (docs/LIVE.md).

The SDK drives the Claude Code CLI, which is logged in with the user's plan. Every ATLAS step (plan,
follow-ups, mission report), every agent task and every consultation is ONE SDK session:

    system prompt  the same texts the api backend sends (role prompt + protocol + node context), written to
                   a temp file (passed as --system-prompt-file: Windows caps command lines at 32k chars)
    prompt         the same user message (task inputs, reports...)
    tools          ours only, as in-process SDK MCP tools (mcp__atlas__*) whose handlers call the store
                   through the same code the api backend uses; plus WebSearch/WebFetch for research agents

Safety (agents run on the user's real computer): every built-in Claude Code tool is removed (`tools=[]`,
plus an explicit deny list), except WebSearch/WebFetch for research agents; `permission_mode="dontAsk"`
denies anything not pre-approved, so nothing ever prompts in a terminal and nothing can escalate; no
filesystem settings are loaded (`setting_sources=[]`: no CLAUDE.md, agents, skills, hooks or MCP servers of
the user), `strict_mcp_config` ignores every other MCP config, --no-chrome drops the browser integration,
prompts are delivered verbatim (no @file expansion), the session runs in an empty temp directory, and no
transcript is written (--no-session-persistence). ANTHROPIC_API_KEY is blanked for the CLI so
it bills the plan, not the API.

Usage: each session's ResultMessage (tokens and the SDK's API-equivalent total_cost_usd) goes to
store.update_usage. A plan usage limit raises UsageLimitError; the orchestrator fails the task with the
reason and closes the mission with what it has.

The SDK boundary is one callable `query(prompt, options, tools)` (tools: {mcp tool name: SdkMcpTool}),
so tests inject `FakeClaudeSDK` instead of spawning the CLI.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import __version__
from ..core.models import AgentReport, Task, TaskStatus
from .agent_loader import ResolvedAgent
from .executor import Validator, invalid_input_message
from .llm import LLMError, UsageLimitError
from .prompts import (
    CONSULT_PROTOCOL,
    PROTOCOL,
    REQUEST_APPROVAL_TOOL,
    SUBMIT_REPORT_TOOL,
    consult_message,
    consult_tool,
    context_block,
    plan_errors_message,
    task_message,
)
from .runtime import AgentRun, MissionScope, _short

log = logging.getLogger("atlas.live")

SERVER = "atlas"
WEB_TOOLS = ("WebSearch", "WebFetch")
WEB_CAPABILITIES = {"research", "market_study"}
# Every built-in Claude Code tool that touches files, the shell, subagents or settings. `tools=[]` already
# removes them all; this deny list is defense in depth (deny rules win over everything).
BUILTIN_TOOLS = (
    "Agent", "Task", "Bash", "BashOutput", "KillShell", "KillBash", "PowerShell", "Monitor",
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "LSP",
    "NotebookRead", "NotebookEdit", "TodoWrite", "TodoRead", "Skill", "SlashCommand",
    "ExitPlanMode", "EnterPlanMode", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree", *WEB_TOOLS,
    "mcp__claude-in-chrome",  # the Claude in Chrome browser integration (also off via --no-chrome)
)
# Extra CLI flags (claude_agent_sdk passes them through; checked against the bundled CLI 2.1.281).
# --no-chrome: otherwise the CLI adds the mcp__claude-in-chrome__* browser tools when the Chrome extension
# is set up. --no-session-persistence: Claude Code keeps no transcript of ATLAS sessions (node context
# stays out of ~/.claude/projects).
EXTRA_ARGS: dict[str, str | None] = {"no-chrome": None, "no-session-persistence": None}
# Long human approvals must not time the MCP call out inside the CLI (milliseconds; 24 h).
MCP_TOOL_TIMEOUT_MS = str(24 * 3600 * 1000)

SDK_TASK_NOTE = """# Runtime (Claude Code session)
Your ATLAS tools are mcp__atlas__consult, mcp__atlas__request_approval and mcp__atlas__submit_report{web}.
You have no file system, shell or any other tool. Finish by calling mcp__atlas__submit_report; once it
succeeds, stop (reply with one short line)."""

SDK_STEP_NOTE = """# Runtime (Claude Code session)
Deliver your answer ONLY by calling the tool mcp__atlas__{name} (no other tools exist). If it returns an
error, fix the input and call it again. Once it succeeds, stop (reply with one short line)."""


def mcp_name(tool: str) -> str:
    return f"mcp__{SERVER}__{tool}"


def _mcp_result(text: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


def _from_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """runtime._tool_result(...) (Messages API shape) → SDK MCP handler result."""
    return _mcp_result(str(result.get("content", "")), error=bool(result.get("is_error")))


def web_enabled(scope: MissionScope, agent: ResolvedAgent) -> bool:
    return scope.config.web_search and bool(WEB_CAPABILITIES & set(agent.agent.capabilities))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

LIMIT_WORDS = re.compile(r"usage limit|rate limit|hit your limit|limit reached|out of (extra )?usage", re.IGNORECASE)
LOGIN_WORDS = re.compile(r"/login|not logged in|log in|invalid api key|authentication|oauth|unauthori[sz]ed", re.IGNORECASE)
LOGIN_MESSAGE = ("Claude Code is not logged in — run `claude` in a terminal and /login with your Max account "
                 "(or set ATLAS_LLM_BACKEND=api)")


def _fmt_reset(ts: float | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    except (OverflowError, OSError, ValueError):
        return None


def limit_reason(rate: Any = None, text: str | None = None) -> str:
    """'Max plan usage limit reached — resets at …' from a RateLimitInfo and/or the CLI's error text."""
    reset = _fmt_reset(getattr(rate, "resets_at", None))
    if reset is None and text and (m := re.search(r"\|(\d{10})\b", text)):  # "Claude AI usage limit reached|<ts>"
        reset = _fmt_reset(int(m.group(1)))
    kind = getattr(rate, "rate_limit_type", None)
    base = "Max plan usage limit reached" + (f" ({kind.replace('_', '-')})" if kind else "")
    if reset:
        return f"{base} — resets at {reset}"
    detail = " ".join((text or "").split())
    if detail and "reset" in detail.lower():
        return f"{base} — {_short(detail, 120)}"
    return base


@dataclass
class _Outcome:
    """What a session saw, to explain a failure."""

    rate: Any = None  # last RateLimitInfo
    assistant_error: str | None = None  # AssistantMessage.error
    assistant_text: str = ""
    result: Any = None  # ResultMessage

    def error(self, exc: BaseException | None = None) -> LLMError:
        texts = [self.assistant_text, getattr(self.result, "result", None) or "",
                 " ".join(getattr(self.result, "errors", None) or []), str(exc or "")]
        text = " ".join(t for t in texts if t).strip()
        status = getattr(self.result, "api_error_status", None) or getattr(exc, "api_error_status", None)
        rejected = getattr(self.rate, "status", None) == "rejected"
        if self.assistant_error == "rate_limit" or rejected or status == 429 or (
            self.assistant_error in (None, "unknown") and LIMIT_WORDS.search(text)
        ):
            return UsageLimitError(limit_reason(self.rate, text))
        if self.assistant_error == "authentication_failed" or status in (401, 403) or (
            self.assistant_error is None and LOGIN_WORDS.search(text) and "api error" not in text.lower()
        ):
            return LLMError(LOGIN_MESSAGE)
        if self.assistant_error == "billing_error":
            return LLMError(f"Claude Code billing error: {_short(text, 200)}")
        try:
            from claude_agent_sdk import CLINotFoundError

            if isinstance(exc, CLINotFoundError):
                return LLMError("Claude Code CLI not found — install Claude Code and log in")
        except ImportError:  # pragma: no cover
            pass
        name = type(exc).__name__ if exc else (self.assistant_error or "error")
        return LLMError(f"Claude Code {name}: {_short(text, 300) or 'session failed'}")


# ---------------------------------------------------------------------------
# The real SDK boundary
# ---------------------------------------------------------------------------

SdkQuery = Callable[[str, Any, dict[str, Any]], AsyncIterator[Any]]


async def sdk_query(prompt: str, options: Any, tools: dict[str, Any]) -> AsyncIterator[Any]:
    """claude_agent_sdk.query (the tools are already inside options.mcp_servers)."""
    from claude_agent_sdk import query

    async for message in query(prompt=prompt, options=options):
        yield message


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class SubscriptionExecutor:
    backend = "subscription"

    def __init__(self, query: SdkQuery | None = None, *, cli_path: str | None = None):
        self._query: SdkQuery = query or sdk_query
        self.cli_path = cli_path if cli_path is not None else (os.getenv("ATLAS_CLAUDE_CLI", "").strip() or None)
        self._root: Path | None = None

    # -- session plumbing ----------------------------------------------------

    @property
    def root(self) -> Path:
        """Neutral temp dir: `cwd/` (empty working directory of every session) and `prompts/`."""
        if self._root is None or not self._root.exists():
            self._root = Path(tempfile.mkdtemp(prefix="atlas-claude-"))
            atexit.register(shutil.rmtree, self._root, ignore_errors=True)
            (self._root / "cwd").mkdir()
            (self._root / "prompts").mkdir()
        return self._root

    def options(self, *, model: str, system_file: str, tools: list[Any], web: bool, max_turns: int) -> Any:
        """ClaudeAgentOptions for one ATLAS session (see the module docstring for the safety rules)."""
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        allowed = [mcp_name(t.name) for t in tools] + (list(WEB_TOOLS) if web else [])
        return ClaudeAgentOptions(
            system_prompt={"type": "file", "path": system_file},
            model=model,
            max_turns=max_turns,
            tools=list(WEB_TOOLS) if web else [],
            allowed_tools=allowed,
            disallowed_tools=[t for t in BUILTIN_TOOLS if not (web and t in WEB_TOOLS)],
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, __version__, tools)} if tools else {},
            cwd=str(self.root / "cwd"),
            cli_path=self.cli_path,
            verbatim_prompts=True,
            extra_args=dict(EXTRA_ARGS),
            env={
                "ANTHROPIC_API_KEY": "",  # bill the logged-in plan, never an API key from .env
                "MCP_TOOL_TIMEOUT": MCP_TOOL_TIMEOUT_MS,
                "CLAUDE_AGENT_SDK_CLIENT_APP": f"atlas/{__version__}",
            },
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
        )

    async def session(self, scope: MissionScope, *, model: str, system: list[str], prompt: str,
                      tools: list[Any] | None = None, web: bool = False, max_turns: int,
                      done: Callable[[], bool] = lambda: False) -> AsyncIterator[Any]:
        """Run one SDK session; yield its top-level AssistantMessages; record its usage.

        Raises UsageLimitError / LLMError when the session fails, unless `done()` says the session already
        delivered what it was for (then trailing errors are only logged).
        """
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock

        tools = tools or []
        path = self.root / "prompts" / f"{uuid4().hex}.md"
        path.write_text("\n\n".join(t for t in system if t), encoding="utf-8")
        seen = _Outcome()
        recorded = False
        try:
            options = self.options(model=model, system_file=str(path), tools=tools, web=web, max_turns=max_turns)
            stream = self._query(prompt, options, {mcp_name(t.name): t for t in tools})
            try:
                async for msg in stream:
                    if isinstance(msg, RateLimitEvent):
                        seen.rate = msg.rate_limit_info
                        if seen.rate.status == "allowed_warning":
                            log.info("Claude plan usage warning: %s", seen.rate)
                    elif isinstance(msg, AssistantMessage):
                        if msg.parent_tool_use_id:
                            continue
                        if msg.error:
                            seen.assistant_error = msg.error
                            seen.assistant_text = " ".join(
                                b.text for b in msg.content if isinstance(b, TextBlock) and b.text)
                            continue
                        yield msg
                    elif isinstance(msg, ResultMessage):
                        seen.result = msg
                        if not recorded:
                            recorded = True
                            await self._record(scope, model, msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if done() or getattr(seen.result, "subtype", None) == "error_max_turns":
                    log.debug("Claude Code session ended with %s after delivering", exc)
                    return
                raise seen.error(exc) from exc
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
        finally:
            path.unlink(missing_ok=True)
        result = seen.result
        if done():
            return
        if result is None:
            raise seen.error(None) if seen.assistant_error else LLMError("Claude Code ended without a result")
        if (result.is_error and result.subtype != "error_max_turns") or seen.assistant_error:
            raise seen.error(None)

    async def _record(self, scope: MissionScope, model: str, result: Any) -> None:
        u = result.usage or {}

        def n(key: str) -> int:
            try:
                return int(u.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        await scope.meter.record_totals(
            model, input_tokens=n("input_tokens"), output_tokens=n("output_tokens"),
            cache_read_tokens=n("cache_read_input_tokens"), cache_write_tokens=n("cache_creation_input_tokens"),
            calls=int(result.num_turns or 1), cost_usd=result.total_cost_usd,
        )

    def tool(self, name: str, description: str, schema: dict[str, Any],
             handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> Any:
        from claude_agent_sdk import SdkMcpTool

        return SdkMcpTool(name=name, description=description, input_schema=schema, handler=handler)

    # -- Executor API --------------------------------------------------------

    async def structured(self, scope: MissionScope, *, model: str, system: list[str], prompt: str,
                         tool: dict[str, Any], max_tokens: int, validate: Validator | None = None,
                         attempts: int = 1) -> dict[str, Any] | None:
        name = tool["name"]
        attempts = max(1, attempts)
        state: dict[str, Any] = {"data": None, "tries": 0, "gave_up": False}

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            if state["data"] is not None or state["gave_up"]:
                return _mcp_result("Already received. Stop now.")
            state["tries"] += 1
            errors = await validate(args) if validate else []
            if not errors:
                state["data"] = args
                return _mcp_result("Received. Your work is done: stop now.")
            if state["tries"] >= attempts:
                state["gave_up"] = True
                return _mcp_result("The input is still invalid; ATLAS will handle it. Stop now.", error=True)
            feedback = plan_errors_message(errors) if name == "create_plan" else invalid_input_message(name, errors)
            return _mcp_result(feedback, error=True)

        mcp_tool = self.tool(name, tool.get("description", ""), tool["input_schema"], handler)
        texts = [*system, SDK_STEP_NOTE.format(name=name)]
        while state["data"] is None and not state["gave_up"] and state["tries"] < attempts:
            before = state["tries"]
            async with aclosing(self.session(
                scope, model=model, system=texts, prompt=prompt, tools=[mcp_tool], max_turns=attempts + 2,
                done=lambda: state["data"] is not None or state["gave_up"],
            )) as stream:
                async for _ in stream:
                    pass
            if state["data"] is None and not state["gave_up"] and state["tries"] == before:
                if validate is None:  # the model never called the tool
                    return None
                state["tries"] += 1
                await validate(None)
        return state["data"]

    async def run_task(self, scope: MissionScope, task: Task, dep_reports: list[str]) -> AgentReport:
        return await SdkAgentRun(scope, task, dep_reports, self).run()

    async def one_shot(self, scope: MissionScope, *, model: str, system: list[str], prompt: str) -> str:
        """A plain answer, no tools (consultations)."""
        from claude_agent_sdk import TextBlock

        parts: list[str] = []
        async with aclosing(self.session(scope, model=model, system=system, prompt=prompt, max_turns=1)) as stream:
            async for msg in stream:
                parts += [b.text for b in msg.content if isinstance(b, TextBlock) and b.text]
        return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# One agent task as one SDK session
# ---------------------------------------------------------------------------


class SdkAgentRun(AgentRun):
    """AgentRun whose loop is Claude Code's; consult / request_approval / submit_report reuse AgentRun's code."""

    def __init__(self, scope: MissionScope, task: Task, dep_reports: list[str], executor: SubscriptionExecutor):
        super().__init__(scope, task, dep_reports)
        self.executor = executor
        self.report: AgentReport | None = None
        self.turns = 0
        self._message_id: str | None = None

    async def _ask(self, target: ResolvedAgent, question: str) -> str:
        sc = self.scope
        return await self.executor.one_shot(
            sc, model=sc.config.models.fast,
            system=[target.role_prompt, CONSULT_PROTOCOL, context_block(sc.context_for(target.agent))],
            prompt=consult_message(sc.objective, self.agent.agent.name, question),
        )

    def _mcp_tools(self) -> list[Any]:
        cfg, ex = self.scope.config, self.executor
        tools = []
        if self._consultable() and cfg.max_consults > 0:
            spec = consult_tool(self._consultable())
            tools.append(ex.tool(spec["name"], spec["description"], spec["input_schema"], self._on_consult))
        for spec, handler in ((REQUEST_APPROVAL_TOOL, self._on_approval), (SUBMIT_REPORT_TOOL, self._on_submit)):
            tools.append(ex.tool(spec["name"], spec["description"], spec["input_schema"], handler))
        return tools

    async def _on_consult(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.report is not None:
            return _mcp_result("Your report is already submitted. Stop now.", error=True)
        return _from_tool_result(await self._consult("sdk", args))

    async def _on_approval(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.report is not None:
            return _mcp_result("Your report is already submitted. Stop now.", error=True)
        return _from_tool_result(await self._request_approval("sdk", args))

    async def _on_submit(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.report is not None:
            return _mcp_result("Your report is already submitted. Stop now.")
        final = self.turns >= self.scope.config.max_turns - 1
        if self.task.requires_approval and not self.approval_requested and not final:
            return _mcp_result("This task requires human approval: call mcp__atlas__request_approval first.",
                               error=True)
        self.report = await self._submit(args)
        return _mcp_result("Report received. Your work on this task is done: stop now.")

    async def run(self) -> AgentReport:
        from claude_agent_sdk import TextBlock, ToolUseBlock

        s, cfg, task, sc = self.store, self.scope.config, self.task, self.scope
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)
        await self._activity(f"Working on '{task.title}'")
        web = web_enabled(sc, self.agent)
        system = [
            self.agent.role_prompt, PROTOCOL,
            SDK_TASK_NOTE.format(web=", plus WebSearch and WebFetch for research" if web else ""),
            context_block(sc.context_for(self.agent.agent)),
        ]
        prompt = task_message(
            sc.objective, sc.node, task, self.dep_reports,
            consultable=[f"{a} ({sc.name(a)})" for a in self._consultable()],
            approval_required=task.requires_approval,
        )
        last_text = ""
        async with aclosing(self.executor.session(
            sc, model=self.agent.model, system=system, prompt=prompt, tools=self._mcp_tools(), web=web,
            max_turns=cfg.max_turns, done=lambda: self.report is not None,
        )) as stream:
            async for msg in stream:
                if msg.message_id is None or msg.message_id != self._message_id:  # the CLI may split a turn
                    self.turns += 1
                self._message_id = msg.message_id
                text = "\n".join(b.text for b in msg.content if isinstance(b, TextBlock) and b.text).strip()
                last_text = text or last_text
                for b in msg.content:
                    if isinstance(b, ToolUseBlock) and b.name in WEB_TOOLS:
                        await self._log_web(b.name, b.input or {})
                if self.report is None and s.task(task.id).status == TaskStatus.IN_PROGRESS.value:
                    progress = round(min(0.9, 0.05 + 0.85 * self.turns / cfg.max_turns), 2)
                    await s.update_task(task.id, progress=progress)
        if self.report is not None:
            return self.report
        return await self._fallback(last_text)

    async def _log_web(self, tool: str, data: dict[str, Any]) -> None:
        what = str(data.get("query") or data.get("url") or "")
        verb = "searched the web" if tool == "WebSearch" else "read a web page"
        await self._activity(f"{'Searching the web' if tool == 'WebSearch' else 'Reading'} · {_short(what, 60)}"
                             if what else "Searching the web")
        await self.store.log(f"{self.agent.agent.name} {verb} · {_short(what)}",
                             mission_id=self.scope.mission_id, agent_id=self.aid)


# ---------------------------------------------------------------------------
# Test double at the SDK boundary
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    prompt: str
    options: Any
    system: str
    tools: dict[str, Any]
    results: list[tuple[str, str, bool]] = field(default_factory=list)  # (tool, result text, is_error)


Step = tuple[Any, ...] | BaseException | Callable[[FakeSession], Any]


def call(tool: str, input: dict[str, Any]) -> Step:
    """The model calls one of our MCP tools (short name, e.g. 'submit_report')."""
    return ("tool", tool, input)


def say(text: str) -> Step:
    return ("text", text)


def builtin(tool: str, input: dict[str, Any]) -> Step:
    """The model uses a built-in tool (e.g. WebSearch); the fake only echoes the tool_use block."""
    return ("builtin", tool, input)


def api_error(kind: str, text: str, *, rate: dict[str, Any] | None = None, status: int | None = None) -> Step:
    """The CLI reports an API error (e.g. 'rate_limit'), ends with an error result and exits non-zero."""
    return ("error", kind, text, rate, status)


class FakeClaudeSDK:
    """Scripted stand-in for claude_agent_sdk.query (no CLI, no network).

    `when(matcher, *scripts)` works like FakeLLM.when: the first rule whose matcher accepts the session
    (a FakeSession: prompt, options, system text, tools) and still has scripts serves the next script.
    A script is a list of steps: call(...) runs our real MCP handler (after the SDK's JSON-schema
    validation), say(...) is assistant text, builtin(...) a built-in tool use, api_error(...) a failure;
    an exception is raised mid-stream. Every session ends with a ResultMessage carrying `usage`.
    """

    def __init__(self, usage: dict[str, int] | None = None, cost: float = 0.01):
        self.rules: list[tuple[Callable[[FakeSession], bool], list[list[Step]], bool]] = []
        self.sessions: list[FakeSession] = []
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}
        self.cost = cost

    def when(self, matcher: Callable[[FakeSession], bool], *scripts: list[Step], repeat: bool = False) -> FakeClaudeSDK:
        self.rules.append((matcher, list(scripts), repeat))
        return self

    def matching(self, pred: Callable[[FakeSession], bool]) -> list[FakeSession]:
        return [x for x in self.sessions if pred(x)]

    async def __call__(self, prompt: str, options: Any, tools: dict[str, Any]) -> AsyncIterator[Any]:
        import jsonschema
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            RateLimitInfo,
            ResultError,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )

        system = Path(options.system_prompt["path"]).read_text(encoding="utf-8")
        sess = FakeSession(prompt, options, system, tools)
        self.sessions.append(sess)
        await asyncio.sleep(0)
        script: list[Step] | None = None
        for matcher, scripts, repeat in self.rules:
            if scripts and matcher(sess):
                script = scripts[0] if (repeat and len(scripts) == 1) else scripts.pop(0)
                break
        if script is None:
            raise LLMError("FakeClaudeSDK: no scripted session for this prompt")
        model = options.model or "fake"

        def result(**kw: Any) -> Any:
            base = {"subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
                        "num_turns": max(1, turns), "session_id": "fake", "usage": dict(self.usage),
                        "total_cost_usd": self.cost, "result": ""}
            return ResultMessage(**(base | kw))

        turns = 0
        for step in script:
            await asyncio.sleep(0)
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step = step(sess)
                if step is None:
                    continue
            turns += 1
            if turns > (options.max_turns or 99):
                yield result(subtype="error_max_turns", is_error=True)
                raise ResultError("Claude Code returned an error result: max turns",
                                  data={"subtype": "error_max_turns", "is_error": True}, exit_code=1)
            kind = step[0]
            if kind == "text":
                yield AssistantMessage([TextBlock(step[1])], model)
            elif kind == "builtin":
                if step[1] not in options.allowed_tools:
                    sess.results.append((step[1], "permission denied", True))
                yield AssistantMessage([ToolUseBlock(f"toolu_{uuid4().hex[:8]}", step[1], step[2])], model)
            elif kind == "tool":
                name = mcp_name(step[1])
                yield AssistantMessage([ToolUseBlock(f"toolu_{uuid4().hex[:8]}", name, step[2])], model)
                tool = tools.get(name)
                if tool is None or name not in options.allowed_tools:
                    sess.results.append((step[1], f"No such tool available: {name}", True))
                    continue
                try:  # like the SDK's run_tool: validation and handler errors become error results
                    jsonschema.validate(step[2], tool.input_schema)
                    out = await tool.handler(step[2])
                    text = "".join(c.get("text", "") for c in out.get("content", []))
                    sess.results.append((step[1], text, bool(out.get("is_error"))))
                except jsonschema.ValidationError as exc:
                    sess.results.append((step[1], f"Input validation error: {exc.message}", True))
                except Exception as exc:  # noqa: BLE001
                    sess.results.append((step[1], str(exc), True))
            elif kind == "error":
                _, err, text, rate, status = step
                if rate:
                    yield RateLimitEvent(RateLimitInfo(**rate), uuid="r", session_id="fake")
                yield AssistantMessage([TextBlock(text)], model, error=err)
                data = {"subtype": "success", "is_error": True, "result": text, "api_error_status": status}
                yield result(is_error=True, result=text, api_error_status=status)
                raise ResultError(f"Claude Code returned an error result: {text}", data=data, exit_code=1)
        yield result()
