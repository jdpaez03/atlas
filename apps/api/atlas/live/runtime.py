"""One agent run: an agent working one task of a live mission (docs/LIVE.md, step 3-6).

The agent only sees its role prompt, the ATLAS protocol, its node context, the mission objective,
its task and the reports of the tasks it depends on. It works in a tool-use loop of at most
`max_turns` turns and ends by calling `submit_report` (otherwise its final text is wrapped into a
low-confidence report).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..core.models import (
    AgentDefinition,
    AgentReport,
    AgentState,
    AgentStatus,
    ApprovalReason,
    ApprovalState,
    Claim,
    ClaimKind,
    Confidence,
    MessageType,
    MissionPhase,
    Task,
    TaskStatus,
)
from ..core.store import StoreError, WorldStore
from .agent_loader import ModelConfig, ResolvedAgent
from .context import NodeContext
from .llm import LLMError, Meter, block_to_param, response_text, tool_uses
from .prompts import (
    CONSULT_PROTOCOL,
    PROTOCOL,
    REQUEST_APPROVAL_TOOL,
    SUBMIT_REPORT_TOOL,
    consult_message,
    consult_tool,
    context_block,
    render_report,
    system_blocks,
    task_message,
    web_search_tool,
)

log = logging.getLogger("atlas.live")

DEFAULT_WEB_SEARCH_TOOL = "web_search_20260318"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class LiveConfig:
    models: ModelConfig = field(default_factory=ModelConfig)
    max_concurrency: int = 4
    max_turns: int = 8
    max_consults: int = 2
    web_search: bool = False
    web_search_tool: str = DEFAULT_WEB_SEARCH_TOOL
    web_search_max_uses: int = 5
    max_tokens: int = 8000
    orchestrator_max_tokens: int = 12000
    consult_max_tokens: int = 1500

    @classmethod
    def from_env(cls, backend: str = "api") -> LiveConfig:
        """Defaults differ per backend: on `subscription` the models are Claude Code aliases, web search is
        on unless ATLAS_WEB_SEARCH=0 (it's included in the plan), and agents get 12 turns (a web search
        is a turn there)."""
        sub = backend == "subscription"
        raw_web = os.getenv("ATLAS_WEB_SEARCH", "").strip().lower()
        return cls(
            models=ModelConfig.from_env(backend),
            max_concurrency=_env_int("ATLAS_MAX_CONCURRENCY", 4),
            max_turns=_env_int("ATLAS_MAX_TURNS", 12 if sub else 8, minimum=2),
            max_consults=_env_int("ATLAS_MAX_CONSULTS", 2, minimum=0),
            web_search=raw_web in ("1", "true", "yes", "on") if raw_web else sub,
            web_search_tool=os.getenv("ATLAS_WEB_SEARCH_TOOL") or DEFAULT_WEB_SEARCH_TOOL,
        )


@dataclass
class MissionScope:
    """Everything an agent run may touch: one mission, one node."""

    store: WorldStore
    meter: Meter
    config: LiveConfig
    context: NodeContext
    mission_id: str
    objective: str
    node: str
    agents: dict[str, ResolvedAgent]  # planned roster (allowed in the node AND available)
    orchestrator: ResolvedAgent
    touched: set[str] = field(default_factory=set)
    consulted: bool = False
    _ctx: dict[str, str] = field(default_factory=dict)

    def context_for(self, agent: AgentDefinition) -> str:
        if agent.id not in self._ctx:
            self._ctx[agent.id] = self.context.load(self.node, agent)
        return self._ctx[agent.id]

    def name(self, agent_id: str) -> str:
        r = self.agents.get(agent_id)
        if r:
            return r.agent.name
        return self.orchestrator.agent.name if agent_id == self.orchestrator.id else agent_id

    async def set_agent(self, agent_id: str, status: AgentStatus | str, *, activity: str | None = None,
                        task_id: str | None = None, with_: list[str] | None = None) -> AgentState:
        self.touched.add(agent_id)
        self.touched.update(with_ or [])
        return await self.store.set_agent_state(
            agent_id, status, mission_id=self.mission_id, activity=activity, task_id=task_id,
            collaborating_with=with_,
        )

    async def on_consult(self) -> None:
        if self.consulted:
            return
        self.consulted = True
        if self.store.mission(self.mission_id).phase == MissionPhase.EXECUTION.value:
            await self.store.set_phase(self.mission_id, MissionPhase.COLLABORATION)


def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return []


def _enum(value: Any, enum: type, default: Any) -> Any:
    try:
        return enum(str(value).strip().upper())
    except ValueError:
        return default


def to_claims(items: Any) -> list[Claim]:
    claims = []
    for raw in items if isinstance(items, list) else []:
        if isinstance(raw, str):
            raw = {"statement": raw}
        if not isinstance(raw, dict) or not str(raw.get("statement", "")).strip():
            continue
        claims.append(
            Claim(
                kind=_enum(raw.get("kind"), ClaimKind, ClaimKind.ASSUMPTION),  # untagged = unverified
                statement=str(raw["statement"]).strip(),
                sources=_str_list(raw.get("sources")),
                confidence=_enum(raw.get("confidence"), Confidence, Confidence.MEDIUM),
            )
        )
    return claims


def _tool_result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        out["is_error"] = True
    return out


def _add_user(messages: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
    """Append user content, merging into the previous user turn if the assistant said nothing."""
    if messages and messages[-1]["role"] == "user":
        prev = messages[-1]["content"]
        if isinstance(prev, str):
            prev = [{"type": "text", "text": prev}]
        messages[-1]["content"] = [*prev, *blocks]
    else:
        messages.append({"role": "user", "content": blocks})


class AgentRun:
    def __init__(self, scope: MissionScope, task: Task, dep_reports: list[str]):
        self.scope = scope
        self.task = task
        self.dep_reports = dep_reports
        self.agent: ResolvedAgent = scope.agents[task.assigned_to or ""]
        self.consults = 0
        self.approval_requested = False

    @property
    def aid(self) -> str:
        return self.agent.id

    @property
    def store(self) -> WorldStore:
        return self.scope.store

    def _consultable(self) -> list[str]:
        return [a for a in self.scope.agents if a != self.aid]

    def _tools(self) -> list[dict[str, Any]]:
        cfg = self.scope.config
        tools: list[dict[str, Any]] = []
        if self._consultable() and cfg.max_consults > 0:
            tools.append(consult_tool(self._consultable()))
        tools += [REQUEST_APPROVAL_TOOL, SUBMIT_REPORT_TOOL]
        if cfg.web_search and "web_search" in self.agent.agent.tools:
            tools.append(web_search_tool(cfg.web_search_tool, cfg.web_search_max_uses))
        return tools

    async def _activity(self, text: str, status: AgentStatus = AgentStatus.WORKING) -> None:
        await self.scope.set_agent(self.aid, status, activity=text, task_id=self.task.id)

    # -- the loop ------------------------------------------------------------

    async def run(self) -> AgentReport:
        s, cfg, task = self.store, self.scope.config, self.task
        await s.update_task(task.id, status=TaskStatus.IN_PROGRESS, progress=0.05)
        await self._activity(f"Working on '{task.title}'")
        system = system_blocks(
            self.agent.role_prompt, PROTOCOL, context_block(self.scope.context_for(self.agent.agent))
        )
        tools = self._tools()
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": task_message(
                self.scope.objective, self.scope.node, task, self.dep_reports,
                consultable=[f"{a} ({self.scope.name(a)})" for a in self._consultable()],
                approval_required=task.requires_approval,
            ),
        }]
        force = False
        nudged = False
        last_text = ""
        for turn in range(cfg.max_turns):
            final = turn == cfg.max_turns - 1
            if turn:
                await s.update_task(task.id, progress=round(min(0.9, 0.05 + 0.85 * turn / cfg.max_turns), 2))
                await self._activity("Drafting report" if (final or force) else f"Working on '{task.title}'")
            kwargs: dict[str, Any] = {
                "model": self.agent.model,
                "max_tokens": cfg.max_tokens,
                "system": system,
                "tools": tools,
                "messages": messages,
            }
            if final or force:
                kwargs["tool_choice"] = {"type": "tool", "name": "submit_report"}
            resp = await self.scope.meter.create(**kwargs)
            content = [block_to_param(b) for b in resp.content]
            if content:
                messages.append({"role": "assistant", "content": content})
            last_text = response_text(resp) or last_text
            await self._log_searches(resp)
            if resp.stop_reason == "pause_turn" and content:
                continue  # a server tool (web search) paused the turn: send it back to resume
            uses = tool_uses(resp)
            if not uses:
                if nudged or final:
                    break
                nudged = force = True
                _add_user(messages, [{"type": "text", "text": "Call submit_report now with your results."}])
                continue
            results: list[dict[str, Any]] = []
            for tu in uses:
                name, data = tu.name, (tu.input if isinstance(tu.input, dict) else {})
                if name == "submit_report":
                    if task.requires_approval and not self.approval_requested and not final:
                        results.append(_tool_result(
                            tu.id, "This task requires human approval: call request_approval first.", error=True
                        ))
                        continue
                    return await self._submit(data)
                if name == "consult":
                    results.append(await self._consult(tu.id, data))
                elif name == "request_approval":
                    results.append(await self._request_approval(tu.id, data))
                else:
                    results.append(_tool_result(tu.id, f"Unknown tool '{name}'.", error=True))
            _add_user(messages, results)
        return await self._fallback(last_text)

    async def _log_searches(self, resp: Any) -> None:
        for b in resp.content:
            if getattr(b, "type", None) == "server_tool_use" and getattr(b, "name", "") == "web_search":
                query = (getattr(b, "input", None) or {}).get("query", "")
                await self._activity(f"Searching the web · {_short(query, 60)}" if query else "Searching the web")
                await self.store.log(
                    f"{self.agent.agent.name} searched the web · {_short(query)}",
                    mission_id=self.scope.mission_id, agent_id=self.aid,
                )

    # -- tools ---------------------------------------------------------------

    async def _consult(self, tool_id: str, data: dict[str, Any]) -> dict[str, Any]:
        sc, s, task = self.scope, self.store, self.task
        target_id = str(data.get("agent_id", "")).strip()
        question = str(data.get("question", "")).strip()
        if self.consults >= sc.config.max_consults:
            return _tool_result(tool_id, f"Consultation limit reached ({sc.config.max_consults}).", error=True)
        if target_id == self.aid or target_id not in sc.agents:
            return _tool_result(tool_id, f"'{target_id}' cannot be consulted in this mission.", error=True)
        if not question:
            return _tool_result(tool_id, "The question is empty.", error=True)
        self.consults += 1
        target = sc.agents[target_id]
        await sc.on_consult()
        request = await s.send_message(
            sc.mission_id, self.aid, target_id, MessageType.REQUEST, _short(question), question,
            task_id=task.id, requires_response=True,
        )
        prev = s.agent_state(target_id)
        await sc.set_agent(self.aid, AgentStatus.COLLABORATING, activity=f"Consulting {target.agent.name}",
                           task_id=task.id, with_=[target_id])
        own_task = prev.current_task_id if prev.current_task_id in {
            t.id for t in s.tasks_for(sc.mission_id)} else None
        mine = await sc.set_agent(target_id, AgentStatus.COLLABORATING,
                                  activity=f"Answering {self.agent.agent.name}", task_id=own_task,
                                  with_=[self.aid])
        ok = True
        try:
            answer = await self._ask(target, question) or "(no answer)"
        except LLMError as exc:
            ok = False
            answer = f"The consultation failed: {exc}"
        finally:
            await self._restore(target_id, prev, mine)
        if ok:
            await s.send_message(
                sc.mission_id, target_id, self.aid, MessageType.ANSWER, f"Re: {_short(question, 70)}", answer,
                task_id=task.id, in_reply_to=request.id,
            )
        await self._activity(f"Working on '{task.title}'")
        return _tool_result(tool_id, answer, error=not ok)

    async def _ask(self, target: ResolvedAgent, question: str) -> str:
        """One LLM call answering a consultation with the target agent's role prompt (fast model, no tools)."""
        sc = self.scope
        resp = await sc.meter.create(
            model=sc.config.models.fast,
            max_tokens=sc.config.consult_max_tokens,
            system=system_blocks(target.role_prompt, CONSULT_PROTOCOL, context_block(sc.context_for(target.agent))),
            messages=[{"role": "user", "content": consult_message(sc.objective, self.agent.agent.name, question)}],
        )
        return response_text(resp)

    async def _restore(self, agent_id: str, prev: AgentState, mine: AgentState) -> None:
        """Put the consulted agent back as it was, unless it changed state in the meantime."""
        now = self.store.agent_state(agent_id)
        if now.updated_at != mine.updated_at or now.status != mine.status:
            return
        try:
            await self.store.set_agent_state(
                agent_id, prev.status, activity=prev.activity, task_id=prev.current_task_id,
                collaborating_with=prev.collaborating_with,
                mission_id=None if prev.current_task_id else self.scope.mission_id,
            )
        except StoreError:
            await self.store.reset_agent(agent_id, mission_id=self.scope.mission_id)

    async def _request_approval(self, tool_id: str, data: dict[str, Any]) -> dict[str, Any]:
        s, task = self.store, self.task
        default = task.approval_reason or ApprovalReason.CONSEQUENTIAL_DECISION
        reason = _enum(data.get("reason"), ApprovalReason, default)
        title = _short(str(data.get("title") or task.title), 120)
        detail = str(data.get("detail") or "").strip() or title
        proposed = str(data.get("proposed_action") or "").strip() or None
        approval = await s.request_approval(
            self.scope.mission_id, self.aid, reason, title, detail, proposed_action=proposed, task_id=task.id
        )
        self.approval_requested = True
        await s.update_task(task.id, status=TaskStatus.AWAITING_APPROVAL)
        await self._activity("Awaiting human approval", AgentStatus.WAITING)
        decided = await s.wait_for_decision(approval.id)
        approved = decided.state == ApprovalState.APPROVED.value
        if s.task(task.id).status == TaskStatus.AWAITING_APPROVAL.value:
            await s.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await self._activity(f"{'Approved' if approved else 'Rejected'} · {title}")
        payload = {"decision": decided.state, "note": decided.decision_note or ""}
        guidance = (
            "The human approved. You may proceed as proposed (you still only prepare/recommend)."
            if approved
            else "The human did NOT approve. Do not perform the action; adapt, and record it in your report."
        )
        return _tool_result(tool_id, json.dumps(payload, ensure_ascii=False) + "\n" + guidance)

    # -- reports -------------------------------------------------------------

    async def _submit(self, data: dict[str, Any]) -> AgentReport:
        s, task = self.store, self.task
        await self._activity("Drafting report")
        limitations = _str_list(data.get("limitations"))
        if task.requires_approval and not self.approval_requested:
            limitations.append("The task required human approval but the agent did not request it.")
        report = await s.submit_report(
            self.scope.mission_id, task.id, self.aid,
            str(data.get("asked_to") or task.title).strip(),
            actions_taken=_str_list(data.get("actions_taken")),
            inputs_used=_str_list(data.get("inputs_used")),
            findings=to_claims(data.get("findings")),
            unresolved=_str_list(data.get("unresolved")),
            needs_agents=_str_list(data.get("needs_agents")),
            confidence=_enum(data.get("confidence"), Confidence, Confidence.MEDIUM),
            limitations=limitations,
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED)
        return report

    async def _fallback(self, last_text: str) -> AgentReport:
        s, task = self.store, self.task
        await s.log(
            f"{self.agent.agent.name} ended without submit_report · wrapping its output",
            mission_id=self.scope.mission_id, agent_id=self.aid,
        )
        statement = last_text.strip()[:4000] or "The agent ended without producing a result."
        report = await s.submit_report(
            self.scope.mission_id, task.id, self.aid, task.title,
            actions_taken=["Worked on the task without submitting a structured report"],
            findings=[Claim(kind=ClaimKind.ASSUMPTION, statement=statement, confidence=Confidence.LOW)],
            unresolved=["The agent's output was not structured into tagged findings"],
            confidence=Confidence.LOW,
            limitations=["Auto-wrapped: the agent did not call submit_report"],
        )
        await s.update_task(task.id, status=TaskStatus.COMPLETED)
        return report


def dependency_inputs(scope: MissionScope, task: Task, refs: dict[str, str]) -> list[str]:
    """Rendered reports of the tasks `task` depends on (and nothing else)."""
    by_task = {r.task_id: r for r in scope.store.reports_for(scope.mission_id)}
    ref_of = {v: k for k, v in refs.items()}
    out = []
    for dep_id in task.depends_on:
        dep = scope.store.task(dep_id)
        report = by_task.get(dep_id)
        if report:
            out.append(render_report(report, title=dep.title, agent_name=scope.name(report.agent_id),
                                     ref=ref_of.get(dep_id)))
    return out
