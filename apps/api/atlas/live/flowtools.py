"""The agents' Power Automate tools (docs/POWER_AUTOMATE.md).

    flow_read(what, ...)        environments, flows, one flow's definition, its run history     (no approval)
    flow_run(flow_id, body?)    run an instant / HTTP-triggered flow                            } each one asks
    flow_toggle(flow_id, on)    turn a flow on or off                                           } the human in
    flow_create(name, ...)      create a cloud flow (Dataverse, solution-aware), as a DRAFT     } ATLAS first

Auth: the same Microsoft 365 app and sign-in as Outlook / OneDrive (MSAL shared cache), delegated, as the user.
    Power Automate API   https://api.flow.microsoft.com, scope https://service.flow.microsoft.com/Flows.*.All
                         (Entra: API permissions -> "Power Automate" -> Flows.Read.All, Flows.Manage.All).
                         Microsoft calls this API unsupported (it is what the portal uses); it reads, runs and
                         toggles every flow the user can see, "My flows" included.
    Dataverse Web API    <ATLAS_FLOW_DATAVERSE_URL>/api/data/v9.2/workflows, scope <url>/user_impersonation
                         (Entra: "Dynamics CRM" -> user_impersonation). The supported way to CREATE flows; it only
                         covers solution-aware flows in an environment with Dataverse.

    ATLAS_FLOW_ENABLED=1          agents get flow_read
    ATLAS_FLOW_WRITE=1            + flow_run / flow_toggle (and flow_create when ATLAS_FLOW_DATAVERSE_URL is set)
    ATLAS_FLOW_ENVIRONMENT        environment id (default: the tenant's default environment)
    ATLAS_FLOW_DATAVERSE_URL      e.g. https://pagadesarrollos.crm.dynamics.com

The approval is inside each write tool: the human sees exactly what will be sent, and only that is sent.
Created flows stay OFF (draft): the human reviews and turns them on in Power Automate.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

FLOW_API = "https://api.flow.microsoft.com"
FLOW_VERSION = "2016-11-01"
FLOW_SCOPES = ["https://service.flow.microsoft.com/Flows.Read.All",
               "https://service.flow.microsoft.com/Flows.Manage.All"]
READ_WHAT = ("environments", "flows", "flow", "runs")
MAX_READ_CHARS = 60_000
TIMEOUT = 60.0
_ID = re.compile(r"^[A-Za-z0-9-]{8,80}$")


class FlowToolError(Exception):
    """A refusal or failure the agent can read."""


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _on("ATLAS_FLOW_ENABLED") and bool(os.getenv("ATLAS_MS_CLIENT_ID", "").strip())


def write_enabled() -> bool:
    return enabled() and _on("ATLAS_FLOW_WRITE")


def dataverse_url() -> str:
    return os.getenv("ATLAS_FLOW_DATAVERSE_URL", "").strip().rstrip("/")


def login_scopes() -> list[str]:
    """For `atlas-graph login --flow` (a sign-in asks one resource at a time)."""
    return list(FLOW_SCOPES)


# -- tool specs -------------------------------------------------------------------------------------------------

_FID = {"type": "string", "description": "the flow's id (name) from flow_read 'flows'"}
_ENV = {"type": "string", "description": "environment id (default: the configured / default environment)"}

FLOW_READ_TOOL: dict[str, Any] = {
    "name": "flow_read",
    "description": (
        "Read Power Automate (live, as the user). what: 'environments' = the environments; 'flows' = the cloud "
        "flows (id, name, on/off, trigger, last modified); 'flow' = one flow's full definition (triggers, actions, "
        "connections: needs flow_id); 'runs' = a flow's recent run history with status and errors (needs flow_id)."
    ),
    "input_schema": {"type": "object", "properties": {
        "what": {"type": "string", "enum": list(READ_WHAT)}, "flow_id": _FID, "environment": _ENV},
        "required": ["what"]},
}
FLOW_RUN_TOOL: dict[str, Any] = {
    "name": "flow_run",
    "description": (
        "Run a Power Automate flow that has an instant trigger ('Manually trigger a flow' / 'When an HTTP request "
        "is received'), with an optional JSON body for its inputs. Asks the human for approval itself (don't call "
        "request_approval) and runs only if approved. Check the result afterwards with flow_read 'runs'."
    ),
    "input_schema": {"type": "object", "properties": {
        "flow_id": _FID, "environment": _ENV,
        "body": {"type": "object", "description": "the trigger's inputs, e.g. {\"text\": \"...\"}"},
        "motivo": {"type": "string", "description": "why, in one line (shown to the human)"}},
        "required": ["flow_id", "motivo"]},
}
FLOW_TOGGLE_TOOL: dict[str, Any] = {
    "name": "flow_toggle",
    "description": ("Turn a Power Automate flow on or off. Asks the human for approval itself (don't call "
                    "request_approval)."),
    "input_schema": {"type": "object", "properties": {
        "flow_id": _FID, "environment": _ENV, "on": {"type": "boolean"},
        "motivo": {"type": "string"}}, "required": ["flow_id", "on", "motivo"]},
}
FLOW_CREATE_TOOL: dict[str, Any] = {
    "name": "flow_create",
    "description": (
        "Create a NEW Power Automate cloud flow (solution-aware, in the Dataverse environment) as a DRAFT (off): the "
        "human reviews it and turns it on in Power Automate. `definition` is the Logic Apps workflow definition "
        "({$schema, contentVersion, parameters: {$connections, $authentication}, triggers, actions}); "
        "`connection_references` maps each connector to an existing connection reference, e.g. "
        "{\"shared_office365\": {\"runtimeSource\": \"embedded\", \"connection\": {\"connectionReferenceLogicalName\": "
        "\"pref_sharedoffice365_x\"}, \"api\": {\"name\": \"shared_office365\"}}}. Start from an existing flow's "
        "definition (flow_read 'flow') whenever you can. Asks the human for approval itself."
    ),
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string"}, "description": {"type": "string"},
        "definition": {"type": "object"}, "connection_references": {"type": "object"},
        "motivo": {"type": "string"}}, "required": ["name", "definition", "motivo"]},
}

NAMES = ("flow_read", "flow_run", "flow_toggle", "flow_create")


def tools() -> list[dict[str, Any]]:
    if not enabled():
        return []
    out = [FLOW_READ_TOOL]
    if write_enabled():
        out += [FLOW_RUN_TOOL, FLOW_TOGGLE_TOOL]
        if dataverse_url():
            out.append(FLOW_CREATE_TOOL)
    return out


def note() -> str:
    if not enabled():
        return ""
    text = "Power Automate: flow_read lists environments and flows, shows a flow's definition and its runs."
    if write_enabled():
        text += (" flow_run / flow_toggle" + (" / flow_create (draft)" if dataverse_url() else "")
                 + " ask the human for approval inside the tool (no separate request_approval).")
    else:
        text += " Changing or running flows is off in this deployment (ATLAS_FLOW_WRITE)."
    return text


# -- HTTP -------------------------------------------------------------------------------------------------------

def _token(scopes: list[str]) -> str:
    from .graphfiles import GraphFilesError, graph_token

    try:
        return graph_token(scopes)
    except GraphFilesError as exc:
        raise FlowToolError(
            "no Microsoft token for Power Automate: grant the app the permission (Entra → API permissions → "
            "Power Automate → Flows.Read.All, Flows.Manage.All, admin consent) and run `atlas-graph login --flow` "
            f"on the ATLAS server ({exc})") from exc


def _request(method: str, url: str, *, token: str | None, body: Any = None, transport: Any = None,
             extra_headers: dict[str, str] | None = None) -> Any:
    headers = {"Accept": "application/json", **(extra_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as c:
            resp = c.request(method, url, headers=headers, json=body if method != "GET" else None)
    except httpx.HTTPError as exc:
        raise FlowToolError(f"{method} {_short_url(url)} failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        text = " ".join(resp.text.split())[:400]
        hint = (" — the token lacks a permission or the user can't see this flow" if resp.status_code in (401, 403)
                else "")
        raise FlowToolError(f"{method} {_short_url(url)}: HTTP {resp.status_code}{hint} · {text}")
    if not resp.content:
        return {"status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "text": resp.text[:2000]}


def _short_url(url: str) -> str:
    return re.sub(r"([?&]sig=)[^&]+", r"\1…", url)[:160]


_TRANSPORT: Any = None  # tests


def _flow(method: str, path: str, body: Any = None) -> Any:
    sep = "&" if "?" in path else "?"
    return _request(method, f"{FLOW_API}{path}{sep}api-version={FLOW_VERSION}", token=_token(FLOW_SCOPES),
                    body=body, transport=_TRANSPORT)


def environment(given: str | None = None) -> str:
    env = (given or os.getenv("ATLAS_FLOW_ENVIRONMENT", "")).strip()
    if env:
        if not _ID.match(env.replace("Default-", "")):
            raise FlowToolError(f"'{env}' is not an environment id")
        return env
    data = _flow("GET", "/providers/Microsoft.ProcessSimple/environments")
    for e in data.get("value", []):
        if (e.get("properties") or {}).get("isDefault"):
            return e["name"]
    raise FlowToolError("no default Power Automate environment found: set ATLAS_FLOW_ENVIRONMENT")


def _fid(v: Any) -> str:
    fid = str(v or "").strip()
    if not _ID.match(fid):
        raise FlowToolError("flow_id must be a flow's id from flow_read 'flows'")
    return fid


def _compact(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= MAX_READ_CHARS else text[:MAX_READ_CHARS] + f"… [truncated, {len(text):,} chars]"


def _summarize_flow(f: dict[str, Any]) -> dict[str, Any]:
    p = f.get("properties") or {}
    trig = ((p.get("definitionSummary") or {}).get("triggers") or [{}])[0]
    return {"id": f.get("name"), "name": p.get("displayName"), "state": p.get("state"),
            "trigger": " ".join(x for x in (trig.get("type"), trig.get("kind")) if x),
            "modified": p.get("lastModifiedTime"), "created": p.get("createdTime")}


def read(data: dict[str, Any]) -> tuple[str, str]:
    """(text for the agent, evidence ref). Synchronous: called in a worker thread."""
    what = str(data.get("what") or "").strip().lower()
    if what not in READ_WHAT:
        raise FlowToolError(f"what must be one of {', '.join(READ_WHAT)}")
    if what == "environments":
        got = _flow("GET", "/providers/Microsoft.ProcessSimple/environments")
        envs = [{"id": e.get("name"), "name": (e.get("properties") or {}).get("displayName"),
                 "default": (e.get("properties") or {}).get("isDefault", False)} for e in got.get("value", [])]
        return f"Power Automate environments:\n{_compact(envs)}", "environments"
    env = environment(data.get("environment"))
    base = f"/providers/Microsoft.ProcessSimple/environments/{env}/flows"
    if what == "flows":
        got = _flow("GET", base)
        flows = [_summarize_flow(f) for f in got.get("value", [])]
        return f"Flows in {env} ({len(flows)}):\n{_compact(flows)}", f"{env}/flows"
    fid = _fid(data.get("flow_id"))
    if what == "flow":
        got = _flow("GET", f"{base}/{fid}")
        p = got.get("properties") or {}
        out = {**_summarize_flow(got), "definition": p.get("definition"),
               "connectionReferences": p.get("connectionReferences")}
        return f"Flow {fid}:\n{_compact(out)}", f"{env}/flows/{fid}"
    got = _flow("GET", f"{base}/{fid}/runs")
    runs = []
    for r in got.get("value", [])[:30]:
        p = r.get("properties") or {}
        runs.append({"run": r.get("name"), "status": p.get("status"), "start": p.get("startTime"),
                     "end": p.get("endTime"), "error": (p.get("error") or {}).get("message")})
    return f"Last runs of {fid} ({len(runs)}):\n{_compact(runs)}", f"{env}/flows/{fid}/runs"


# -- writes -----------------------------------------------------------------------------------------------------

@dataclass
class WritePlan:
    kind: str
    title: str
    detail: str
    proposed_action: str
    ref: str
    args: dict[str, Any]


def plan_write(name: str, data: dict[str, Any]) -> WritePlan:
    if not write_enabled():
        raise FlowToolError("Changing Power Automate is off (ATLAS_FLOW_WRITE=1 in ATLAS's .env turns it on)")
    motivo = " ".join(str(data.get("motivo") or "").split())[:400]
    if not motivo:
        raise FlowToolError("motivo is required: say why, in one line")
    if name in ("flow_run", "flow_toggle"):
        env = environment(data.get("environment"))
        fid = _fid(data.get("flow_id"))
        try:
            flow = _flow("GET", f"/providers/Microsoft.ProcessSimple/environments/{env}/flows/{fid}")
        except FlowToolError as exc:
            raise FlowToolError(f"can't find flow {fid}: {exc}") from exc
        label = (flow.get("properties") or {}).get("displayName") or fid
        if name == "flow_run":
            body = data.get("body") if isinstance(data.get("body"), dict) else {}
            shown = json.dumps(body, ensure_ascii=False, indent=1)[:3000] if body else "(sin datos de entrada)"
            return WritePlan("run", f"Power Automate · ejecutar «{label}»",
                             f"{motivo}\n\nEntradas:\n{shown}",
                             f"Ejecutar el flujo {label} ({fid})", f"{env}/flows/{fid}/run",
                             {"env": env, "fid": fid, "body": body, "label": label})
        on = bool(data.get("on"))
        return WritePlan("toggle", f"Power Automate · {'encender' if on else 'apagar'} «{label}»", motivo,
                         f"{'start' if on else 'stop'} {label} ({fid})", f"{env}/flows/{fid}/{'start' if on else 'stop'}",
                         {"env": env, "fid": fid, "on": on, "label": label})
    if name == "flow_create":
        dv = dataverse_url()
        if not dv:
            raise FlowToolError("creating flows needs ATLAS_FLOW_DATAVERSE_URL (a Dataverse environment)")
        fname = " ".join(str(data.get("name") or "").split())[:100]
        definition = data.get("definition")
        if not fname or not isinstance(definition, dict):
            raise FlowToolError("flow_create needs a name and a definition object")
        if not isinstance(definition.get("triggers"), dict) or not definition["triggers"]:
            raise FlowToolError("the definition needs at least one trigger")
        if not isinstance(definition.get("actions"), dict):
            raise FlowToolError("the definition needs an actions object")
        definition.setdefault("$schema", "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/"
                                         "2016-06-01/workflowdefinition.json#")
        definition.setdefault("contentVersion", "1.0.0.0")
        definition.setdefault("parameters", {"$connections": {"defaultValue": {}, "type": "Object"},
                                             "$authentication": {"defaultValue": {}, "type": "SecureObject"}})
        refs = data.get("connection_references") if isinstance(data.get("connection_references"), dict) else {}
        clientdata = {"properties": {"connectionReferences": refs, "definition": definition},
                      "schemaVersion": "1.0.0.0"}
        trig = ", ".join(definition["triggers"])
        acts = ", ".join(list(definition["actions"])[:15]) + (" …" if len(definition["actions"]) > 15 else "")
        detail = (f"{motivo}\n\nNombre: {fname}\nDisparador: {trig}\nAcciones ({len(definition['actions'])}): {acts}\n"
                  f"Conectores: {', '.join(refs) or '(ninguno)'}\n\nSe crea APAGADO (borrador) en {dv}: tú lo revisas "
                  "y lo enciendes en Power Automate → Soluciones.")
        return WritePlan("create", f"Power Automate · crear flujo «{fname}»", detail,
                         f"Crear el flujo {fname} en Dataverse (apagado)", f"{dv}/workflows",
                         {"dv": dv, "body": {"category": 5, "type": 1, "primaryentity": "none", "name": fname,
                                             "description": str(data.get("description") or "")[:2000],
                                             "clientdata": json.dumps(clientdata, ensure_ascii=False)}})
    raise FlowToolError(f"unknown Power Automate tool {name}")


def execute(plan: WritePlan) -> str:
    a = plan.args
    if plan.kind == "toggle":
        _flow("POST", f"/providers/Microsoft.ProcessSimple/environments/{a['env']}/flows/{a['fid']}/"
                      f"{'start' if a['on'] else 'stop'}")
        return f"Power Automate: «{a['label']}» is now {'ON' if a['on'] else 'OFF'}."
    if plan.kind == "run":
        cb = _flow("POST", f"/providers/Microsoft.ProcessSimple/environments/{a['env']}/flows/{a['fid']}/"
                           "triggers/manual/listCallbackUrl")
        url = str(cb.get("response", {}).get("value") or cb.get("value") or "")
        if not url.startswith("https://"):
            raise FlowToolError("this flow has no instant trigger to call (only 'Manually trigger a flow' or "
                                "'When an HTTP request is received' flows can be run from here)")
        # a signed (sig=) callback must NOT also carry a bearer token; an Entra-protected one needs it
        token = None if "sig=" in url else _token(FLOW_SCOPES)
        res = _request("POST", url, token=token, body=a["body"] or {}, transport=_TRANSPORT)
        return (f"Power Automate: «{a['label']}» was started (HTTP {res.get('status', 'ok') if isinstance(res, dict) else 'ok'})."
                " Check its result with flow_read 'runs'.")
    if plan.kind == "create":
        dv = a["dv"]
        res = _request("POST", f"{dv}/api/data/v9.2/workflows", token=_token([f"{dv}/user_impersonation"]),
                       body=a["body"], transport=_TRANSPORT, extra_headers={"Prefer": "return=representation"})
        wid = res.get("workflowid") if isinstance(res, dict) else None
        return (f"Power Automate: flow «{a['body']['name']}» created as a DRAFT (off)"
                + (f", workflowid {wid}" if wid else "") + ". The human turns it on in Power Automate → Solutions.")
    raise FlowToolError(f"unknown plan {plan.kind}")
