/**
 * Mock ARGOS (docs/ARGOS.md): seeded alerts, Rocks and one L10 brief, plus "Run checks now" and "Build now"
 * that play out through events like the real engine. URL switches for demos:
 *   ?suite=none    → the PAGA Suite isn't configured (the L10 check is "not configured", no L10 alerts)
 *   ?suite=consent → configured but not signed in: the L10 chip offers Connect (device code, connected after ~6 s),
 *                    then an L10-only run adds the 2 L10 alerts
 *   ?argos=failed → the dashboards check failed on its last run
 *   ?argos=none   → the backend has no ARGOS module (GET /argos/status → 404)
 */
import type { AlertStatus, ArgosApi, ArgosCheckStatus, ArgosStatus, InboxConnectStart, InboxConnectState } from "./api";
import type { AgentState, AgentStatus, Alert, AlertEvidence, Attachment, Brief, EventType, Mission, RockStatus } from "./contracts";

type Emit = (type: EventType, payload: Record<string, unknown>, summary: string, agent: string | null, missionId?: string | null) => void;

export interface MockArgosHooks {
  /** register a mission with the engine (history) and emit it */
  putMission(m: Mission, type: EventType, summary: string): void;
  /** a downloadable file (blob URL) */
  deliverable(name: string, content: string, mime?: string): Attachment;
}

const NODE = "corporate";
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
let n = 0;
const rid = (p: string) => `${p}_${Date.now().toString(36)}${(n++).toString(36)}`;

function param(k: string): string | null {
  return typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get(k);
}

/** ISO week label, e.g. 2026-W39. */
export function isoWeek(d = new Date()): string {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y = t.getUTCFullYear();
  const w = Math.ceil(((t.getTime() - Date.UTC(y, 0, 1)) / DAY + 1) / 7);
  return `${y}-W${String(w).padStart(2, "0")}`;
}
const weekNo = (offsetWeeks = 0) => Number(isoWeek(new Date(Date.now() + offsetWeeks * WEEK)).slice(-2));

/** Next weekday slot among "09:30,16:00" (local time). */
function nextRun(): string {
  const slots = [
    [9, 30],
    [16, 0],
  ];
  const d = new Date();
  for (let i = 0; i < 8; i++) {
    const day = new Date(d.getFullYear(), d.getMonth(), d.getDate() + i);
    if (day.getDay() === 0 || day.getDay() === 6) continue;
    for (const [h, m] of slots) {
      const t = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
      if (t.getTime() > d.getTime()) return t.toISOString();
    }
  }
  return iso(DAY);
}
function nextBrief(): string {
  const d = new Date();
  for (let i = 0; i < 8; i++) {
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate() + i, 7, 30);
    if (t.getDay() === 1 && t.getTime() > d.getTime()) return t.toISOString();
  }
  return iso(WEEK);
}
const hhmm = (s: string) => new Date(s).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });

/* ---------------------------------------------------------------- alerts */

function alert(p: Omit<Alert, "node" | "fingerprint" | "status" | "first_seen" | "last_seen" | "mission_id"> & Partial<Alert>): Alert {
  return {
    node: NODE,
    status: "OPEN",
    fingerprint: `${p.check}:${p.project ?? "-"}:${p.kind}:${p.title.toLowerCase().replace(/\W+/g, "-").slice(0, 40)}`,
    first_seen: iso(-6 * HOUR),
    last_seen: iso(-6 * HOUR),
    mission_id: null,
    ...p,
  };
}
const q = (source: string, version: AlertEvidence["version"], quote: string): AlertEvidence => ({ source, version, quote });

function seedAlerts(suite: boolean): Alert[] {
  const w = weekNo();
  const pw = weekNo(-1);
  const list: Alert[] = [
    alert({
      id: "alr_tn_entrega",
      check: "dashboards",
      kind: "moved_date",
      severity: "HIGH",
      project: "Torre Norte",
      title: "La entrega de la Torre B se movió de octubre a diciembre",
      detail: "La fecha de entrega a clientes se recorrió 8 semanas entre un reporte y otro, sin nota que lo explique. Afecta 24 escrituras programadas para noviembre.",
      evidence: [
        q(`Dashboard Torre Norte S${pw}.pdf`, "previous", "Entrega a clientes Torre B: 16/10/2026 · Estatus: en tiempo"),
        q(`Dashboard Torre Norte S${w}.pdf`, "current", "Entrega a clientes Torre B: 11/12/2026 · Estatus: reprogramada"),
      ],
      first_seen: iso(-26 * HOUR),
      last_seen: iso(-6 * HOUR),
    }),
    alert({
      id: "alr_tn_escrituradas",
      check: "dashboards",
      kind: "kpi_mismatch",
      severity: "MEDIUM",
      project: "Torre Norte",
      title: "El KPI de escrituradas no cuadra con la tabla de unidades",
      detail: "El resumen reporta 22 unidades escrituradas, pero la tabla de unidades lista 19 con estatus «Escriturada».",
      evidence: [
        q(`Dashboard Torre Norte S${w}.pdf`, "single", "Unidades escrituradas: 22/51"),
        q(`Dashboard Torre Norte S${w}.pdf`, "single", "Tabla «Estatus por unidad»: 19 filas con estatus Escriturada, 3 con estatus Firma agendada"),
      ],
    }),
    alert({
      id: "alr_rb_identico",
      check: "dashboards",
      kind: "identical_report",
      severity: "MEDIUM",
      project: "Residencial Bosque",
      title: `El reporte de la S${w} es idéntico al de la S${pw}`,
      detail: "Mismo contenido (mismo text hash) que la semana pasada: probablemente se reenvió el archivo anterior sin actualizar.",
      status: "ACKNOWLEDGED",
      evidence: [
        q(`Reporte semanal Bosque S${pw}.xlsx`, "previous", "Avance de obra: 47% · Ventas acumuladas: 112 de 180 · Cobranza del mes: $6.2 M"),
        q(`Reporte semanal Bosque S${w}.xlsx`, "current", "Avance de obra: 47% · Ventas acumuladas: 112 de 180 · Cobranza del mes: $6.2 M"),
      ],
      first_seen: iso(-30 * HOUR),
      last_seen: iso(-6 * HOUR),
    }),
    alert({
      id: "alr_pp_faltante",
      check: "dashboards",
      kind: "missing_report",
      severity: "HIGH",
      project: "Plaza Poniente",
      title: `No llegó el dashboard de Plaza Poniente de la S${w}`,
      detail: `Plaza Poniente envió reporte las 3 semanas anteriores; esta semana no hay ningún archivo. Último recibido: S${pw}.`,
      evidence: [q(`Dashboard Plaza Poniente S${pw}.pdf`, "single", `Plaza Poniente · Reporte semanal S${pw} · Comercialización y obra`)],
      first_seen: iso(-6 * HOUR),
    }),
  ];
  if (suite) {
    list.push(
      alert({
        id: "alr_l10_todo",
        check: "l10",
        kind: "overdue_todo",
        severity: "HIGH",
        project: "Residencial Bosque",
        title: "To-do vencido hace 9 días: enviar planos corregidos al DRO",
        detail: "Responsable: M. Herrera. Sigue abierto con 40% de avance y sin reporte esta semana.",
        evidence: [q("PAGA Suite · /l10/admin/todos", "single", "Enviar planos corregidos al DRO · responsable: M. Herrera · fecha: 2026-09-15 · avance_pct: 40 · estado: abierto")],
        first_seen: iso(-3 * DAY),
      }),
      alert({
        id: "alr_l10_issue",
        check: "l10",
        kind: "stale_issue",
        severity: "MEDIUM",
        project: "Plaza Poniente",
        title: "Issue abierto hace 23 días sin decisor: renegociar la renta del ancla",
        detail: "Lleva más de 14 días en el Issues List y nadie tiene asignada la decisión.",
        evidence: [q("PAGA Suite · /l10/issues", "single", "Renegociar renta del local ancla · decide: — · abierto_desde: 2026-09-01")],
        first_seen: iso(-8 * DAY),
      }),
    );
  }
  return list;
}

/* ---------------------------------------------------------------- Rocks */

interface RockSeed {
  id: string;
  title: string;
  owner: string;
  project: string | null;
  due: string;
  metric: string | null;
  target: number | null;
  current: number | null;
  start_value: number | null;
  start_date: string | null;
  done?: boolean;
}

const QUARTER = "2026-Q3";
const round1 = (x: number) => Math.round(x * 10) / 10;

/** The rules from docs/ARGOS.md § Rocks. */
export function evaluateRock(r: RockSeed, now = Date.now()): RockStatus {
  const base = {
    id: r.id,
    title: r.title,
    owner: r.owner,
    project: r.project,
    quarter: QUARTER,
    due: r.due,
    metric: r.metric,
    target: r.target,
    current: r.current,
    start_value: r.start_value,
    start_date: r.start_date,
    updated_at: new Date(now).toISOString(),
  };
  const measurable = r.metric != null && r.target != null;
  let observed: number | null = null;
  let required: number | null = null;
  if (measurable && r.current != null) {
    const start = r.start_date ? new Date(r.start_date).getTime() : now - 8 * WEEK;
    const elapsed = Math.max(1 / 7, (now - start) / WEEK);
    const left = Math.max(1 / 7, (new Date(r.due).getTime() - now) / WEEK);
    observed = round1((r.current - (r.start_value ?? 0)) / elapsed);
    required = round1(Math.max(0, r.target! - r.current) / left);
  }
  if (r.done) return { ...base, status: "DONE", reason: "Marked done", required_pace: null, observed_pace: observed };
  if (new Date(r.due).getTime() < now) return { ...base, status: "FAILED", reason: "Overdue and not done", required_pace: required, observed_pace: observed };
  if (!measurable) return { ...base, status: "UNKNOWN", reason: "needs a measurable", required_pace: null, observed_pace: null };
  if (r.current != null && r.current >= r.target!) return { ...base, status: "ON_TRACK", reason: "Target reached; mark it done", required_pace: 0, observed_pace: observed };
  if (observed != null && required != null && observed < 0.5 * required) {
    return { ...base, status: "AT_RISK", reason: `Observed pace ${observed}/wk is under half the required ${required}/wk`, required_pace: required, observed_pace: observed };
  }
  return { ...base, status: "ON_TRACK", reason: `On pace: ${observed}/wk vs ${required}/wk needed`, required_pace: required, observed_pace: observed };
}

function seedRocks(): RockSeed[] {
  const start = iso(-8 * WEEK);
  const due = iso(5 * WEEK);
  return [
    { id: "rock_escrituras_tn", title: "Escriturar 51 unidades de Torre Norte", owner: "L. Ramírez", project: "Torre Norte", due, metric: "unidades escrituradas", target: 51, current: 34, start_value: 0, start_date: start },
    { id: "rock_ventas_rb", title: "Vender 40 departamentos en Residencial Bosque", owner: "A. Morales", project: "Residencial Bosque", due, metric: "departamentos vendidos", target: 40, current: 14, start_value: 2, start_date: start },
    { id: "rock_ancla_pp", title: "Firmar el contrato del ancla de Plaza Poniente", owner: "J. Castillo · R. Vega", project: "Plaza Poniente", due: iso(-9 * DAY), metric: "contrato firmado", target: 1, current: 0, start_value: 0, start_date: start },
    { id: "rock_tablero_cob", title: "Implementar el tablero de cobranza por torre", owner: "S. Navarro", project: null, due, metric: null, target: null, current: null, start_value: null, start_date: start },
    { id: "rock_cartera", title: "Recuperar $18 M de cartera vencida", owner: "P. Domínguez", project: null, due, metric: "MDP cobrados", target: 18, current: 18, start_value: 0, start_date: start, done: true },
  ];
}

/* ---------------------------------------------------------------- brief */

function briefText(b: Brief): string {
  const lines = [`L10 brief · ${b.week}`, "", ...b.headline.map((h) => `• ${h}`), ""];
  for (const [k, v] of Object.entries(b.sections)) lines.push(k.toUpperCase(), ...v.map((x) => `  - ${x}`), "");
  lines.push("(Simulated: the real API serves a .docx.)");
  return lines.join("\n");
}

function makeBrief(hooks: MockArgosHooks, week: string, createdAt: string, suite: boolean): Brief {
  const b: Brief = {
    id: rid("brf"),
    node: NODE,
    week,
    headline: [
      "2 de 5 Rocks en tiempo; el ancla de Plaza Poniente venció sin firmarse.",
      "La entrega de la Torre B se recorrió a diciembre en el dashboard, sin explicación.",
      suite ? "Un to-do del L10 lleva 9 días vencido y un issue 23 días sin decisor." : "El L10 no se revisó: la PAGA Suite no está configurada.",
    ],
    sections: {
      Rocks: [
        "2 de 5 en tiempo (1 terminado).",
        "Fallido · Firmar el contrato del ancla de Plaza Poniente (J. Castillo · R. Vega): venció hace 9 días. Tiene dos dueños.",
        "En riesgo · Vender 40 departamentos en Residencial Bosque: ritmo de 1.5/sem contra 5.2/sem necesario.",
        "Sin medible · Implementar el tablero de cobranza por torre.",
      ],
      Dashboards: [
        "Torre Norte: entrega de Torre B movida del 16/10 al 11/12; escrituradas 22 en el KPI contra 19 en la tabla.",
        "Residencial Bosque: reporte idéntico al de la semana pasada (reconocido).",
        "Plaza Poniente: no llegó el dashboard de esta semana.",
      ],
      L10: suite
        ? ["1 to-do vencido: enviar planos corregidos al DRO (M. Herrera, 9 días).", "1 issue estancado: renegociar la renta del ancla (23 días, sin decisor)."]
        : ["Sin datos: la PAGA Suite no está configurada."],
      "Follow-ups": ["2 compromisos tuyos vencidos: calendario de escrituración de Torre B y autorización de la estimación 7."],
    },
    deliverable: null,
    mission_id: null,
    created_at: createdAt,
  };
  b.deliverable = hooks.deliverable(`L10 brief ${week} (simulated).txt`, briefText(b), "text/plain");
  return b;
}

/* ---------------------------------------------------------------- engine */

export interface MockArgos {
  alerts: Alert[];
  rocks: RockStatus[];
  briefs: Brief[];
  /** ARGOS's idle state: MONITORING with its activity line */
  idleState: AgentState;
  api: ArgosApi;
}

export function mockArgos(emit: Emit, speed: number, hooks: MockArgosHooks): MockArgos {
  const suiteParam = param("suite");
  const consent = suiteParam === "consent";
  let suite = suiteParam !== "none" && !consent;
  const mode = param("argos");
  const alerts = new Map(seedAlerts(suite).map((a) => [a.id, a]));
  let l10Seeded = suite;
  let suiteConnectStarted = 0;
  const rockSeeds = new Map(seedRocks().map((r) => [r.id, r]));
  const rocks = new Map([...rockSeeds.values()].map((r) => [r.id, evaluateRock(r)]));
  const briefs: Brief[] = [makeBrief(hooks, isoWeek(), iso(-2 * DAY - 3 * HOUR), suite)];

  const lastRun = iso(-6 * HOUR);
  const checks: ArgosCheckStatus[] = [
    mode === "failed"
      ? { name: "dashboards", enabled: true, last_run: lastRun, last_ok: false, note: "Microsoft Graph returned 401: the mailbox token expired. Reconnect the inbox." }
      : { name: "dashboards", enabled: true, last_run: lastRun, last_ok: true, note: "3 projects · 5 dashboards read" },
    suite
      ? { name: "l10", enabled: true, last_run: lastRun, last_ok: true, note: "PAGA Suite · 14 to-dos, 6 issues" }
      : consent
        ? {
            name: "l10",
            enabled: false,
            state: "not configured",
            last_run: null,
            last_ok: null,
            hint: "Connect PAGA Suite: sign in with your Microsoft account so ATLAS can read the L10 (read-only).",
            note: "Connect PAGA Suite: sign in with your Microsoft account so ATLAS can read the L10 (read-only).",
          }
        : {
            name: "l10",
            enabled: false,
            state: "not configured",
            last_run: null,
            last_ok: null,
            hint: "Set ATLAS_SUITE_URL and ATLAS_SUITE_SCOPE to read the L10 from PAGA Suite.",
            note: "Set ATLAS_SUITE_URL and ATLAS_SUITE_SCOPE to read the L10 from PAGA Suite.",
          },
    { name: "rocks", enabled: true, last_run: lastRun, last_ok: true, note: "5 Rocks from rocks.yaml" },
  ];
  let status: ArgosStatus = { checks, next_run: nextRun(), next_brief: nextBrief(), running: false, mission_id: null };
  let ran = false;

  const idleActivity = () => `Watching dashboards, L10, Rocks · next check ${hhmm(status.next_run ?? iso(HOUR))}`;
  const state = (s: AgentStatus, activity: string | null): AgentState => ({ agent_id: "argos", status: s, current_task_id: null, activity, collaborating_with: [], updated_at: iso(0) });
  const setArgos = (s: AgentStatus, activity: string, missionId: string | null) =>
    emit("agent.state_changed", { state: state(s, activity) }, `ARGOS → ${s} · ${activity}`, "argos", missionId);
  const later = (ms: number, fn: () => void) => setTimeout(fn, ms / speed);

  const putAlert = (a: Alert, summary: string, missionId: string | null) => {
    alerts.set(a.id, a);
    emit("alert.upserted", { alert: a }, summary, "argos", missionId);
  };
  const putRock = (r: RockStatus, summary: string, missionId: string | null) => {
    rocks.set(r.id, r);
    emit("rock.updated", { rock: r }, summary, "argos", missionId);
  };

  const newMission = (objective: string): Mission => ({
    id: rid("msn"),
    objective,
    node: NODE,
    mode: "simulated",
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 },
    context: "ARGOS",
    phase: "EXECUTION",
    priority: "MEDIUM",
    task_ids: [],
    final_report_id: null,
    attachments: [],
    round: 1,
    interrupted: false,
    created_at: iso(0),
    closed_at: null,
  });
  const close = (m: Mission, summary: string) => hooks.putMission({ ...m, phase: "CLOSED", closed_at: iso(0) }, "mission.closed", summary);

  const api: ArgosApi = {
    async status() {
      if (mode === "none") return null;
      return structuredClone(status);
    },
    async run(only?: string[]) {
      if (mode === "none") throw new Error("ATLAS API 404: Not Found");
      if (status.running) throw new Error('ATLAS API 409: {"detail":"An ARGOS run is already in progress."}');
      const wanted = only?.length ? only : status.checks.filter((c) => c.enabled).map((c) => c.name);
      const off = status.checks.find((c) => wanted.includes(c.name) && !c.enabled);
      if (off) throw new Error(`ATLAS API 409: ${JSON.stringify({ detail: off.note })}`);
      const m = newMission(`ARGOS watch · ${wanted.join(", ")} · ${hhmm(iso(0))}`);
      hooks.putMission(m, "mission.created", `ARGOS watch started: ${wanted.join(", ")}`);
      status = { ...status, running: true, mission_id: m.id };
      const dash = wanted.includes("dashboards");
      setArgos("WORKING", dash ? "Reading this week's dashboards from email" : `Running ${wanted.join(", ")}`, m.id);
      const first = dash && !ran;
      if (dash) ran = true;
      const addL10 = suite && wanted.includes("l10") && !l10Seeded;
      if (addL10) l10Seeded = true;
      const pp = `Dashboard Plaza Poniente S${weekNo()}.pdf`;
      const ppPrev = `Dashboard Plaza Poniente S${weekNo(-1)}.pdf`;
      later(1300, () => {
        if (dash) setArgos("WORKING", `Comparing ${pp} with last week`, m.id);
        if (first) {
          putAlert(
            alert({
              id: "alr_pp_local",
              check: "dashboards",
              kind: "removed_row",
              severity: "MEDIUM",
              project: "Plaza Poniente",
              title: "El local L-07 desapareció de la tabla de comercialización",
              detail: "La semana pasada aparecía apartado; esta semana ya no está en la tabla ni en el total de locales.",
              evidence: [q(ppPrev, "previous", "L-07 · 86 m² · Apartado · Cliente: Farmacias del Centro"), q(pp, "current", "Locales comercializados: 17 de 24 (L-01 a L-06, L-08 a L-18)")],
              first_seen: iso(0),
              last_seen: iso(0),
              mission_id: m.id,
            }),
            "ARGOS raised MEDIUM · Plaza Poniente: El local L-07 desapareció de la tabla de comercialización",
            m.id,
          );
        }
      });
      later(2500, () => {
        const missing = alerts.get("alr_pp_faltante");
        if (first && missing && missing.status === "OPEN") {
          putAlert({ ...missing, status: "RESOLVED", last_seen: iso(0), mission_id: m.id }, `Resolved · ${missing.title} (no longer detected)`, m.id);
        }
        if (dash) {
          for (const id of ["alr_tn_entrega", "alr_tn_escrituradas"]) {
            const a = alerts.get(id);
            if (a && a.status !== "RESOLVED") putAlert({ ...a, last_seen: iso(0) }, `Still detected · ${a.title}`, m.id);
          }
        }
        if (suite && wanted.includes("l10")) {
          setArgos("WORKING", "Reading L10 to-dos and issues from PAGA Suite", m.id);
          if (addL10) {
            for (const a of seedAlerts(true).filter((x) => x.check === "l10")) {
              const sev = a.severity;
              putAlert({ ...a, first_seen: iso(0), last_seen: iso(0), mission_id: m.id }, `ARGOS raised ${sev} · ${a.project ?? "L10"}: ${a.title}`, m.id);
            }
          }
        }
      });
      later(3400, () => {
        if (!wanted.includes("rocks")) return;
        setArgos("WORKING", "Recomputing Rock statuses", m.id);
        for (const seed of rockSeeds.values()) {
          const r = evaluateRock(seed);
          if (r.status !== "DONE") putRock(r, `Rock ${r.status.replace("_", " ").toLowerCase()} · ${r.title}`, m.id);
        }
      });
      later(4300, () => {
        const at = iso(0);
        status = {
          ...status,
          running: false,
          mission_id: null,
          next_run: nextRun(),
          checks: status.checks.map((c) =>
            c.enabled && wanted.includes(c.name)
              ? { ...c, state: "ok", last_run: at, last_ok: true, note: c.name === "dashboards" ? "3 projects · 6 dashboards read" : c.name === "l10" ? "PAGA Suite · 14 to-dos, 6 issues" : c.note }
              : c,
          ),
        };
        close(m, first ? "ARGOS watch done · 1 new, 1 resolved" : addL10 ? "ARGOS watch done · 2 new" : "ARGOS watch done · no changes");
        setArgos("MONITORING", idleActivity(), m.id);
      });
      return m;
    },
    async patchAlert(id: string, st: AlertStatus) {
      const a = alerts.get(id);
      if (!a) throw new Error("ATLAS API 404: alert not found");
      const next: Alert = { ...a, status: st };
      putAlert(next, `Alert ${st.toLowerCase()} · ${a.title}`, null);
      return next;
    },
    async reloadRocks() {
      for (const seed of rockSeeds.values()) putRock(evaluateRock(seed), `Rock reloaded · ${seed.title}`, null);
      return { ok: true, count: rockSeeds.size };
    },
    async patchRock(id: string, current: number) {
      const seed = rockSeeds.get(id);
      if (!seed) throw new Error("ATLAS API 404: rock not found");
      if (!Number.isFinite(current)) throw new Error('ATLAS API 422: {"detail":"current must be a number"}');
      const next = { ...seed, current };
      rockSeeds.set(id, next);
      const r = evaluateRock(next);
      putRock(r, `Rock updated · ${r.title}: ${current}${r.target != null ? ` of ${r.target}` : ""}`, null);
      return r;
    },
    async buildBrief() {
      if (mode === "none") throw new Error("ATLAS API 404: Not Found");
      const m = newMission(`ARGOS · L10 brief ${isoWeek()}`);
      hooks.putMission(m, "mission.created", `ARGOS is building the L10 brief ${isoWeek()}`);
      setArgos("WORKING", `Drafting the L10 brief ${isoWeek()}`, m.id);
      later(2800, () => {
        const b = { ...makeBrief(hooks, isoWeek(), iso(0), suite), mission_id: m.id };
        briefs.push(b);
        emit("brief.ready", { brief: b }, `L10 brief ${b.week} is ready`, "argos", m.id);
        close(m, `L10 brief ${b.week} ready`);
        setArgos("MONITORING", idleActivity(), m.id);
      });
      return m;
    },
    async briefs() {
      return [...briefs];
    },
    briefFileUrl: (b) => b.deliverable?.download_url ?? undefined,
    async suiteConnect(): Promise<InboxConnectStart> {
      if (!consent) throw new Error('ATLAS API 409: {"detail":"ATLAS_SUITE_SCOPE is not set. Add the PAGA Suite API scope to .env first."}');
      suiteConnectStarted = Date.now();
      return {
        user_code: "PG4-7KXM",
        verification_uri: "https://microsoft.com/devicelogin",
        expires_in: 900,
        message: "To sign in, open https://microsoft.com/devicelogin and enter the code PG4-7KXM to authenticate.",
      };
    },
    async suiteConnectStatus(): Promise<InboxConnectState> {
      const hold = param("hold") === "1";
      if (suite) return { state: "connected", account: "usuario@empresa.example.mx", error: null };
      if (!hold && suiteConnectStarted && Date.now() - suiteConnectStarted > 6000 / speed) {
        suite = true;
        status = {
          ...status,
          checks: status.checks.map((c) =>
            c.name === "l10" ? { ...c, enabled: true, state: "never run", hint: null, note: "PAGA Suite · signed in as usuario@empresa.example.mx" } : c,
          ),
        };
        return { state: "connected", account: "usuario@empresa.example.mx", error: null };
      }
      return { state: "pending", account: null, error: null };
    },
  };

  return {
    alerts: mode === "none" ? [] : [...alerts.values()],
    rocks: mode === "none" ? [] : [...rocks.values()],
    briefs: mode === "none" ? [] : briefs.slice(),
    idleState: mode === "none" ? state("IDLE", null) : state("MONITORING", idleActivity()),
    api,
  };
}
