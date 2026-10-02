# Agents and Power Automate

Builder: `apps/api/atlas/live/flowtools.py`. Same Microsoft 365 app and sign-in as Outlook / OneDrive, delegated: the agents act as you and see the flows you see.

## What agents can do

| Tool | What it does | Approval |
|---|---|---|
| `flow_read(what)` | Reads one of: `environments`, `flows` (id, name, on/off, trigger, last change), `flow` (one flow's full definition: triggers, actions, connections) or `runs` (a flow's recent runs, with status and error). | none |
| `flow_run(flow_id, body?)` | Runs a flow with an instant trigger ("Manually trigger a flow" or "When an HTTP request is received"), optionally passing its inputs. | inside the tool |
| `flow_toggle(flow_id, on)` | Turns a flow on or off. | inside the tool |
| `flow_create(name, definition, connection_references)` | Creates a new cloud flow in Dataverse, **turned off** (a draft). | inside the tool |

**Approvals.**
- The approval shows exactly what will be sent: the inputs, the flow, the trigger and the actions.
- A rejection changes nothing.
- The planner adds no separate approval task for these tools.

**Created flows stay off.** You review them and turn them on yourself, in Power Automate → Solutions.

**Evidence.** Every call is recorded as `external_call` ("Power Automate …").

## Microsoft's limits (worth knowing)

- **Reading, running and turning flows on or off** uses `api.flow.microsoft.com`, the API the Power Automate portal itself uses.
  - It covers every flow you can see, "My flows" included.
  - Microsoft labels it unsupported, so it could change without notice.
- **Creating flows** uses the supported route, Dataverse.
  - It only covers **solution-aware** flows in an environment that has Dataverse.
  - Flows under "My flows" can't be created by code.
  - The definition is the Logic Apps workflow JSON plus connection references to connections that already exist. The agent should start from an existing flow's definition (`flow_read 'flow'`) rather than writing one from nothing.
- **Running a flow** only works for instant triggers. Flows that run on a schedule, or start from an email or a SharePoint change, can be turned on or off, not run.

## Setup

1. **Entra.** Open App registrations → the ATLAS app (`ATLAS_MS_CLIENT_ID`) → API permissions → Add → **APIs my organization uses**:
   - **Power Automate** (or "Microsoft Flow Service"), delegated: `Flows.Read.All` and `Flows.Manage.All`.
   - Only to create flows: **Dynamics CRM**, delegated: `user_impersonation`.
   - Then **Grant admin consent**.
2. **Server `.env`:**
   ```
   ATLAS_FLOW_ENABLED=1
   ATLAS_FLOW_WRITE=1                     # run / turn on-off / create (each asks you first)
   # ATLAS_FLOW_ENVIRONMENT=Default-…     # optional; default = the tenant's default environment
   # ATLAS_FLOW_DATAVERSE_URL=https://<org>.crm.dynamics.com   # only to create flows
   ```
3. **Sign in.** One sign-in serves one Microsoft resource, so Power Automate and Dataverse each get their own:
   ```
   cd ~/atlas/apps/api
   uv run atlas-graph login --flow
   uv run atlas-graph login --dataverse     # only if you set ATLAS_FLOW_DATAVERSE_URL
   uv run atlas-graph status                # shows "Power Automate: OK"
   ```
4. **Restart:** `sudo systemctl restart atlas-api`.

**Finding the Dataverse URL.** Go to Power Platform admin center → Environments → your environment → **Environment URL**. If it shows none, the environment has no Dataverse and agents can't create flows there. They can still read, run and turn on or off.
