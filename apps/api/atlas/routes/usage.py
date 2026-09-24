"""Usage and estimated cost across live missions (Phase 5).

  GET /usage?days=30&node=corporate  -> {days, node, missions, totals, by_agent, by_day[], backend, note}

Totals come from each mission's `usage` and `usage_by_agent` (recorded per LLM call by the Meter). A mission's
usage is attributed to the local day it started. On the subscription backend the dollar figures are
API-equivalent estimates of what the same work would cost on the API, not charges on the plan.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query

from ..core.models import Usage
from .deps import LiveDep, StoreDep

router = APIRouter(tags=["usage"])

NOTES = {
    "subscription": "Runs on your Claude plan: dollar figures are API-equivalent estimates, not charges. "
                    "Each mission's usage counts on the day it started.",
    "api": "Estimated from the price table; your Anthropic invoice is the source of truth. "
           "Each mission's usage counts on the day it started.",
}


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens, output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens, llm_calls=a.llm_calls + b.llm_calls,
        est_cost_usd=round(a.est_cost_usd + b.est_cost_usd, 6),
    )


def _local_day(ts: datetime) -> date:
    return ts.astimezone().date()


@router.get("/usage")
def get_usage(store: StoreDep, live: LiveDep, days: int = Query(30, ge=1, le=366),
              node: str | None = None) -> dict:
    today = datetime.now().astimezone().date()
    start = today - timedelta(days=days - 1)
    totals = Usage()
    by_agent: dict[str, Usage] = {}
    by_day: dict[date, Usage] = {start + timedelta(days=i): Usage() for i in range(days)}
    n = 0
    for m in store.snapshot().missions:
        if m.mode != "live" or (node and m.node != node):
            continue
        day = _local_day(m.created_at)
        if day < start or day > today:
            continue
        n += 1
        totals = _add(totals, m.usage)
        by_day[day] = _add(by_day[day], m.usage)
        for agent_id, u in (m.usage_by_agent or {}).items():
            by_agent[agent_id] = _add(by_agent.get(agent_id, Usage()), u)
    backend = live.backend_info().backend
    return {
        "days": days,
        "node": node,
        "missions": n,
        "totals": totals.model_dump(),
        "by_agent": {k: v.model_dump() for k, v in sorted(by_agent.items(), key=lambda kv: -kv[1].est_cost_usd)},
        "by_day": [{"date": d.isoformat(), "est_cost_usd": u.est_cost_usd, "llm_calls": u.llm_calls}
                   for d, u in sorted(by_day.items())],
        "backend": backend,
        "note": NOTES.get(backend or "", "Estimated usage of live missions. "
                                         "Each mission's usage counts on the day it started."),
    }
