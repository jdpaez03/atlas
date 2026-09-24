"""ARGOS L10 check (docs/ARGOS.md § L10): the PAGA Suite client over httpx.MockTransport, deterministic findings."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from atlas.argos.checks import CheckContext, CheckNotConfigured
from atlas.argos.l10 import L10Check
from atlas.argos.suite import NOT_CONFIGURED_HINT, SuiteClient, SuiteError, map_issue, map_todo, parse_date
from atlas.core.events import EventBus
from atlas.core.registry import AgentRegistry
from atlas.core.store import WorldStore

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)  # Thu 11:00 in Mexico City · week 2026-W39 (Mon 21 Sep)

TODOS = {
    "semana_id": 39,
    "data": [
        # overdue 16 days, not done, reported this week
        {"id": 1, "titulo": "Enviar flujo actualizado", "responsable": {"nombre": "Ana"}, "fecha": "2026-09-08",
         "estado": "pendiente", "avance_pct": 40, "semaforo": "rojo", "reportes": [{"semana_id": 39}]},
        # due next week, not reported this week (only week 38)
        {"id": 2, "titulo": "Cotizar elevadores", "responsable": "Luis", "fecha": "2026-10-01",
         "estado": "en proceso", "reportes": [{"semana_id": 38, "fecha": "2026-09-15"}]},
        # done (even though past due): nothing
        {"id": 3, "titulo": "Firmar contrato", "responsable": "Ana", "fecha": "2026-09-01", "estado": "Completado",
         "reportes": [{"semana_id": 39}]},
        # unknown field names: Todo_ID / Descripción / Asignado_A / Fecha_Limite / Reportado
        {"Todo_ID": "x-9", "Descripción": "Revisar permisos", "Asignado_A": "Pedro", "Fecha_Limite": "20/09/2026",
         "Reportado": "no", "campo_raro": 1},
        # not overdue, reported: nothing
        {"id": 5, "titulo": "Plan de ventas", "responsable": "Rosa", "fecha": "2026-10-10",
         "reportado": True},
        # garbage record: skipped
        {"foo": "bar"},
    ],
}

ISSUES = [
    {"id": "i1", "issue": "Retraso en licencia", "responsable": "Ana", "decide": "Juan",
     "abierto_desde": "2026-08-01", "status": "abierto"},  # 54 days: HIGH (> 2×14)
    {"id": "i2", "issue": "Proveedor de acero", "responsable": "Luis", "decide": "",
     "created_at": "2026-09-20T10:00:00Z", "status": "open"},  # young, no decider: LOW
    {"id": "i3", "issue": "Cambio de layout", "decide": "Juan", "created_at": "2026-09-01", "estado": "abierto"},
    # 23 days: MEDIUM
    {"id": "i4", "issue": "Tema cerrado", "decide": "Juan", "abierto_desde": "2026-01-01", "estado": "Resuelto"},
    {"id": "i5", "issue": "Tema reciente", "decide": "Juan", "abierto_desde": "2026-09-22", "estado": "abierto"},
]


def handler(todos=TODOS, issues=ISSUES, seen: list | None = None, status: dict | None = None):
    def h(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if status and path in status:
            return httpx.Response(status[path], text="Internal Server Error: db down")
        if path.endswith("/l10/admin/todos"):
            return httpx.Response(200, json=todos)
        if path.endswith("/l10/issues"):
            return httpx.Response(200, json=issues)
        if "/l10/resumen/" in path:
            return httpx.Response(200, json={"semana": 39, "total": 5})  # no unreported list
        return httpx.Response(404)

    return h


def client(**kw) -> SuiteClient:
    seen = kw.pop("seen", None)
    return SuiteClient("https://suite.test/api/", "tok-123", transport=httpx.MockTransport(handler(seen=seen, **kw)))


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry.load()


def ctx(registry, config=None) -> CheckContext:
    return CheckContext(store=WorldStore(registry, EventBus()), scope=None, mail=None, config=config or {}, now=NOW)


def run(check, c):
    return asyncio.run(check.run(c))


def by_fp(result):
    return {a.fingerprint: a for a in result.alerts}


def test_l10_findings(registry):
    seen: list[httpx.Request] = []
    result = run(L10Check(client(seen=seen)), ctx(registry))
    assert result.ok
    alerts = by_fp(result)

    over = alerts["l10:overdue-todo:1"]
    assert over.kind == "overdue_todo" and over.severity == "HIGH"  # 16 days
    assert "Ana" in over.detail and "2026-09-08" in over.detail and "40% done" in over.detail
    assert over.evidence[0].source == "PAGA Suite /l10/admin/todos"
    assert "Enviar flujo actualizado" in over.evidence[0].quote
    assert "l10:unreported-todo:1" not in alerts  # reported for week 39

    unrep = alerts["l10:unreported-todo:2"]
    assert unrep.severity == "LOW" and "week 39" in unrep.detail and "2026-09-15" in unrep.detail
    assert "l10:overdue-todo:2" not in alerts

    # unknown field names mapped (Todo_ID, Descripción, Asignado_A, day-first Fecha_Limite, Reportado)
    x9 = alerts["l10:overdue-todo:x-9"]
    assert x9.title == "Overdue to-do · Revisar permisos" and "Pedro" in x9.detail and x9.severity == "LOW"
    assert "l10:unreported-todo:x-9" in alerts

    assert not any(":3" in fp or ":5" in fp for fp in alerts if "todo" in fp)  # done / fine

    assert alerts["l10:stale-issue:i1"].severity == "HIGH"
    assert "open 54 days" in alerts["l10:stale-issue:i1"].detail
    assert alerts["l10:stale-issue:i2"].severity == "LOW" and "no decider" in alerts["l10:stale-issue:i2"].detail
    assert alerts["l10:stale-issue:i3"].severity == "MEDIUM"
    assert "l10:stale-issue:i4" not in alerts and "l10:stale-issue:i5" not in alerts

    assert result.notes[0].startswith("5 to-dos (4 open) · 2 overdue · 2 unreported · 4 open issues (3 stale")

    # read-only, authenticated, resumen for the envelope week
    assert {r.method for r in seen} == {"GET"}
    assert all(r.headers["Authorization"] == "Bearer tok-123" for r in seen)
    assert [r.url.path for r in seen] == ["/api/l10/admin/todos", "/api/l10/issues", "/api/l10/resumen/39"]


def test_stale_days_from_watch_yaml(registry):
    result = run(L10Check(client()), ctx(registry, {"l10": {"stale_issue_days": 60}}))
    alerts = by_fp(result)
    assert "l10:stale-issue:i1" not in alerts and "l10:stale-issue:i3" not in alerts
    assert "l10:stale-issue:i2" in alerts  # still no decider


def test_resumen_unreported_list_wins(registry):
    def h(request: httpx.Request) -> httpx.Response:
        if "/resumen/" in request.url.path:
            return httpx.Response(200, json={"sin_reporte": [{"id": 5}]})
        return handler()(request)

    c = SuiteClient("https://suite.test/api", "t", transport=httpx.MockTransport(h))
    alerts = by_fp(run(L10Check(c), ctx(registry)))
    assert {fp for fp in alerts if "unreported" in fp} == {"l10:unreported-todo:5"}


def test_http_500_is_a_clear_failure(registry):
    c = client(status={"/api/l10/admin/todos": 500})
    with pytest.raises(SuiteError) as exc:
        run(L10Check(c), ctx(registry))
    msg = str(exc.value)
    assert "PAGA Suite GET /l10/admin/todos failed: HTTP 500" in msg and "db down" in msg


def test_auth_refused_and_timeout(registry):
    c = client(status={"/api/l10/issues": 401})
    with pytest.raises(SuiteError, match="refused \\(HTTP 401\\): check ATLAS_SUITE_TOKEN"):
        run(L10Check(c), ctx(registry))

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    c = SuiteClient("https://suite.test", "t", timeout=3, transport=httpx.MockTransport(slow))
    with pytest.raises(SuiteError, match="timed out after 3s"):
        run(L10Check(c), ctx(registry))

    c = SuiteClient("https://suite.test", "t", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(SuiteError, match="did not return JSON"):
        run(L10Check(c), ctx(registry))


def test_not_configured(registry, monkeypatch):
    monkeypatch.delenv("ATLAS_SUITE_URL", raising=False)
    monkeypatch.setenv("ATLAS_SUITE_TOKEN", "x")
    with pytest.raises(CheckNotConfigured) as exc:
        run(L10Check(), ctx(registry))
    assert exc.value.hint == NOT_CONFIGURED_HINT
    assert "ATLAS_SUITE_URL" in str(exc.value)


def test_custom_auth_header(monkeypatch):
    monkeypatch.setenv("ATLAS_SUITE_URL", "https://suite.test/api")
    monkeypatch.setenv("ATLAS_SUITE_TOKEN", "abc")
    monkeypatch.setenv("ATLAS_SUITE_AUTH_HEADER", "X-Api-Key")
    seen: list[httpx.Request] = []
    c = SuiteClient.from_env(transport=httpx.MockTransport(handler(seen=seen)))
    todos, week = asyncio.run(c.todos())
    assert week == "39" and len(todos) == 5
    assert seen[0].headers["X-Api-Key"] == "abc" and "Authorization" not in seen[0].headers


def test_mapping_helpers():
    assert parse_date("2026-09-08T10:00:00Z") == date(2026, 9, 8)
    assert parse_date("08/09/2026") == date(2026, 9, 8)
    assert parse_date(1788000000000) is not None and parse_date("nope") is None
    t = map_todo({"ID": 7, "Nombre": "X", "Status": "DONE", "Avance": 0.5})
    assert t is not None and t.done and t.progress_pct == 50
    assert map_todo({"x": 1}) is None
    i = map_issue({"id": 1, "tema": "T", "quien_decide": {"name": "Juan"}, "fecha_alta": "2026-09-01",
                   "cerrado": False})
    assert i is not None and i.decider == "Juan" and not i.closed and i.opened == date(2026, 9, 1)
    wrapped = json.loads(json.dumps({"data": {"issues": [{"id": 1, "titulo": "A"}]}}))
    from atlas.argos.suite import unwrap

    assert unwrap(wrapped, "issues") == [{"id": 1, "titulo": "A"}]


def test_preflight(monkeypatch):
    monkeypatch.delenv("ATLAS_SUITE_URL", raising=False)
    monkeypatch.delenv("ATLAS_SUITE_TOKEN", raising=False)
    with pytest.raises(CheckNotConfigured):
        L10Check().preflight({})
    L10Check(client()).preflight({})
