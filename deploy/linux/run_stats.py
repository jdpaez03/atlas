#!/usr/bin/env python3
"""Read-only run statistics from ATLAS's event log (no repo imports, no writes).

    python3 ~/atlas/deploy/linux/run_stats.py                 # last 10 live missions
    python3 ~/atlas/deploy/linux/run_stats.py --last 30
    python3 ~/atlas/deploy/linux/run_stats.py --db ~/atlas-local/atlas.db --mission msn_ab12

Per mission: wall time, time per phase, tasks per agent (time, retries, revisions, auto-wrapped reports),
approval wait, audit verdicts and token/cost usage by agent. Then totals across all missions shown.
The database is opened read-only (sqlite URI mode=ro), so it is safe to run while atlas-api is up.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

KINDS = ("mission", "task", "report", "approval", "audit", "evidence")


def ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def secs(a: str | None, b: str | None) -> float | None:
    x, y = ts(a), ts(b)
    return (y - x).total_seconds() if x and y else None


def fmt(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def load(db: Path, mission: str | None) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sql = "SELECT json FROM events WHERE mission_id IS NOT NULL"
    args: list[str] = []
    if mission:
        sql += " AND mission_id = ?"
        args.append(mission)
    missions: dict[str, dict] = defaultdict(lambda: {k: {} for k in KINDS} | {"phases": []})
    for (raw,) in conn.execute(sql + " ORDER BY seq", args):
        e = json.loads(raw)
        m = missions[e["mission_id"]]
        payload = e.get("payload") or {}
        for kind in KINDS:
            obj = payload.get(kind)
            if isinstance(obj, dict) and obj.get("id"):
                m[kind][obj["id"]] = obj  # latest version wins
        mo = payload.get("mission")
        if isinstance(mo, dict) and mo.get("phase"):
            ph = m["phases"]
            if not ph or ph[-1][0] != mo["phase"]:
                ph.append((mo["phase"], e["ts"]))
        m["last_ts"] = e["ts"]
    conn.close()
    return missions


def wrapped(report: dict) -> bool:
    return any("auto-wrapped" in (x or "").lower() for x in report.get("limitations") or [])


def detail(m: dict) -> None:
    """What happened inside the mission: each task with its outcome, approvals and recorded actions, in order."""
    tasks = sorted(m["task"].values(), key=lambda t: t.get("created_at") or "")
    reports = {r.get("task_id"): r for r in m["report"].values() if r.get("task_id")}
    for t in tasks:
        r = reports.get(t["id"]) or {}
        print(f"\n  · [{t.get('status')}] {t.get('assigned_to')} · {t.get('title', '')[:90]}"
              f"{' (revisión)' if t.get('revision_of') else ''} · {fmt(secs(t.get('started_at'), t.get('completed_at')))}")
        for x in (r.get("limitations") or [])[:4]:
            print(f"      límite: {x[:160]}")
        for ev in sorted((e for e in m["evidence"].values() if e.get("task_id") == t["id"]),
                         key=lambda e: e.get("at") or ""):
            print(f"      {'ok ' if ev.get('ok', True) else 'ERR'} {ev.get('kind')}: {ev.get('ref', '')[:110]}"
                  + (f" · {ev.get('detail', '')[:90]}" if ev.get("detail") else ""))
    for a in sorted(m["approval"].values(), key=lambda a: a.get("created_at") or ""):
        print(f"\n  aprobación [{a.get('state')}] {a.get('title', '')[:100]}"
              + (f" · nota: {a['decision_note'][:80]}" if a.get("decision_note") else ""))


def mission_stats(mid: str, m: dict, totals: dict) -> None:
    mo = next(iter(m["mission"].values()), None)
    if not mo or mo.get("mode") != "live":
        return
    end = mo.get("closed_at") or m.get("last_ts")
    print(f"\n=== {mid} · {mo.get('node')} · {mo.get('phase')} · ronda {mo.get('round')}")
    print(f"    {mo.get('objective', '')[:110]!r}")
    print(f"    duración total: {fmt(secs(mo.get('created_at'), end))}"
          f"{'' if mo.get('closed_at') else ' (sin cerrar)'}")

    phases = m["phases"]
    per_phase: dict[str, float] = defaultdict(float)
    for i, (name, start) in enumerate(phases):
        stop = phases[i + 1][1] if i + 1 < len(phases) else end
        per_phase[name] += secs(start, stop) or 0
    if per_phase:
        print("    fases: " + " · ".join(f"{k} {fmt(v)}" for k, v in per_phase.items() if v >= 1))
    for k, v in per_phase.items():
        totals["phase"][k] += v

    by_agent: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for t in m["task"].values():
        a = by_agent[t.get("assigned_to") or "?"]
        a["tareas"] += 1
        a["seg"] += secs(t.get("started_at"), t.get("completed_at")) or 0
        a["reintentos"] += t.get("retries") or 0
        a["revisiones"] += 1 if t.get("revision_of") else 0
        a["fallidas"] += 1 if t.get("status") in ("FAILED", "BLOCKED", "CANCELLED") else 0
    for r in m["report"].values():
        if "task_id" in r:
            by_agent[r.get("agent_id") or "?"]["auto_wrapped"] += 1 if wrapped(r) else 0
    usage = mo.get("usage_by_agent") or {}
    if by_agent or usage:
        print(f"    {'agente':<16}{'tareas':>7}{'tiempo':>9}{'reint':>6}{'revis':>6}{'fall':>5}"
              f"{'wrap':>5}{'in_tok':>10}{'cache':>10}{'out_tok':>9}{'llm':>5}{'US$':>8}")
    for agent in sorted(set(by_agent) | set(usage), key=lambda x: -(usage.get(x, {}).get("est_cost_usd") or 0)):
        a, u = by_agent[agent], usage.get(agent, {})
        print(f"    {agent:<16}{int(a['tareas']):>7}{fmt(a['seg']):>9}{int(a['reintentos']):>6}"
              f"{int(a['revisiones']):>6}{int(a['fallidas']):>5}{int(a['auto_wrapped']):>5}"
              f"{u.get('input_tokens', 0):>10,}{u.get('cache_read_tokens', 0):>10,}{u.get('output_tokens', 0):>9,}"
              f"{u.get('llm_calls', 0):>5}{u.get('est_cost_usd', 0):>8.2f}")
        t = totals["agent"][agent]
        for k in ("tareas", "seg", "reintentos", "revisiones", "fallidas", "auto_wrapped"):
            t[k] += a[k]
        for k in ("input_tokens", "cache_read_tokens", "output_tokens", "llm_calls", "est_cost_usd"):
            t[k] += u.get(k, 0) or 0
    tot = mo.get("usage") or {}
    print(f"    total: {tot.get('llm_calls', 0)} llamadas · in {tot.get('input_tokens', 0):,} · "
          f"cache {tot.get('cache_read_tokens', 0):,} · out {tot.get('output_tokens', 0):,} · "
          f"US$ {tot.get('est_cost_usd', 0):.2f}")

    waits = [secs(a.get("created_at"), a.get("decided_at")) for a in m["approval"].values()]
    waits = [w for w in waits if w is not None]
    active = max(0.0, (secs(mo.get("created_at"), end) or 0) - per_phase.get("CLOSED", 0) - sum(waits))
    print(f"    tiempo activo (sin CLOSED entre rondas ni espera de aprobaciones): {fmt(active)}")
    totals["active"] += active
    if m["approval"]:
        print(f"    aprobaciones: {len(m['approval'])} · espera total {fmt(sum(waits))}"
              f" · máx {fmt(max(waits) if waits else None)}")
        totals["approvals"] += len(m["approval"])
        totals["approval_wait"] += sum(waits)
    verdicts: dict[str, int] = defaultdict(int)
    for au in m["audit"].values():
        verdicts[au.get("verdict", "?")] += 1
        totals["verdicts"][au.get("verdict", "?")] += 1
    if verdicts:
        print("    auditoría: " + " · ".join(f"{k} {v}" for k, v in verdicts.items()))
    totals["missions"] += 1
    totals["wall"] += secs(mo.get("created_at"), end) or 0
    totals["cost"] += tot.get("est_cost_usd", 0) or 0


def main() -> None:
    home = Path(os.environ.get("ATLAS_LOCAL_DIR", Path.home() / "atlas-local"))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=home / "atlas.db")
    p.add_argument("--last", type=int, default=10, help="how many recent missions (default 10)")
    p.add_argument("--mission", help="one mission id")
    p.add_argument("--detail", action="store_true", help="also list each task's actions, limits and approvals")
    args = p.parse_args()
    if not args.db.exists():
        raise SystemExit(f"no database at {args.db} (use --db)")

    missions = load(args.db, args.mission)
    live = [k for k, v in missions.items()
            if any(mo.get("mode") == "live" for mo in v["mission"].values())]
    live.sort(key=lambda k: next(iter(missions[k]["mission"].values())).get("created_at") or "")
    chosen = live[-args.last:] if not args.mission else live

    totals: dict = {"missions": 0, "wall": 0.0, "active": 0.0, "cost": 0.0, "approvals": 0, "approval_wait": 0.0,
                    "phase": defaultdict(float), "verdicts": defaultdict(int),
                    "agent": defaultdict(lambda: defaultdict(float))}
    for mid in chosen:
        mission_stats(mid, missions[mid], totals)
        if args.detail:
            detail(missions[mid])

    n = totals["missions"]
    if not n:
        print("no live missions found")
        return
    print(f"\n######## TOTAL · {n} misiones · {fmt(totals['wall'])} · US$ {totals['cost']:.2f}"
          f" · promedio {fmt(totals['wall'] / n)} y US$ {totals['cost'] / n:.2f} por misión")
    print(f"  tiempo activo: {fmt(totals['active'])} · promedio {fmt(totals['active'] / n)} por misión")
    totals["phase"].pop("CLOSED", None)
    wall = sum(totals["phase"].values()) or 1
    print("  tiempo por fase: " + " · ".join(
        f"{k} {fmt(v)} ({v / wall:.0%})" for k, v in sorted(totals["phase"].items(), key=lambda x: -x[1]) if v >= 1))
    if totals["approvals"]:
        print(f"  aprobaciones: {totals['approvals']} · espera {fmt(totals['approval_wait'])}")
    if totals["verdicts"]:
        print("  auditoría: " + " · ".join(f"{k} {v}" for k, v in totals["verdicts"].items()))
    cost = totals["cost"] or 1
    print("  por agente (costo · % · tareas · tiempo · reint/revis/fall/wrap · cache hit):")
    for agent, t in sorted(totals["agent"].items(), key=lambda x: -x[1]["est_cost_usd"]):
        seen = t["input_tokens"] + t["cache_read_tokens"]
        hit = t["cache_read_tokens"] / seen if seen else 0
        print(f"    {agent:<16} US$ {t['est_cost_usd']:>7.2f} {t['est_cost_usd'] / cost:>4.0%}"
              f" · {int(t['tareas'])} · {fmt(t['seg'])} · {int(t['reintentos'])}/{int(t['revisiones'])}/"
              f"{int(t['fallidas'])}/{int(t['auto_wrapped'])} · cache {hit:.0%}")


if __name__ == "__main__":
    main()
