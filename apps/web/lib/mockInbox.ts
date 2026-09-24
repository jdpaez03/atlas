/**
 * Mock inbox (docs/INBOX.md) — seeded follow-ups and drafts, a connected Graph mailbox, and a "Scan now"
 * that finds one more follow-up via events. URL switches for demos:
 *   ?inbox=disconnected  → Graph configured but not signed in (exercises the device-code flow)
 *   ?inbox=unconfigured  → no mail source configured
 *   ?inbox=none          → the backend has no inbox module (404 → the chip hides)
 *   ?digest=none         → no seeded CC digest (empty Digest tab)
 */
import type { DraftDecisionBody, FollowUpPatch, InboxApi, InboxConnectStart, InboxConnectState, InboxStatus } from "./api";
import type { Digest, EmailDraft, EventType, FollowUp, Mission, MissionReport } from "./contracts";
import { digestFollowup, scanDigest, seedDigest } from "./mockDigest";

type Emit = (type: EventType, payload: Record<string, unknown>, summary: string, agent: string | null, missionId?: string | null) => void;

const NODE = "corporate";
const HOUR = 3600_000;
const DAY = 24 * HOUR;
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
/** A due date `days` from today at 18:00 local. */
function dueIn(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  d.setHours(18, 0, 0, 0);
  return d.toISOString();
}

let n = 0;
const rid = (p: string) => `${p}_${Date.now().toString(36)}${(n++).toString(36)}`;
const OUTLOOK = "https://outlook.office.com/mail/";

function fu(p: Omit<FollowUp, "node" | "created_at" | "updated_at" | "mission_id" | "draft_id" | "status"> & Partial<FollowUp>): FollowUp {
  const created = p.source?.received_at ?? iso(-DAY);
  return { node: NODE, status: "OPEN", draft_id: null, mission_id: null, created_at: created, updated_at: created, ...p };
}

function seed(): { followups: FollowUp[]; drafts: EmailDraft[] } {
  const followups: FollowUp[] = [
    fu({
      id: "fup_escr_torreb",
      kind: "MY_COMMITMENT",
      title: "Enviar calendario de escrituración de Torre B",
      detail: "Me comprometí a mandar las fechas de firma por departamento para que la notaría agende.",
      counterpart: "Notaría 14 — Mesa de escrituras <escrituras@notaria14.example.mx>",
      due: dueIn(-2),
      priority: "HIGH",
      source: {
        message_id: "m1",
        subject: "RE: Escrituración Torre B — fechas de firma",
        sender: "Notaría 14 <escrituras@notaria14.example.mx>",
        received_at: iso(-5 * DAY),
        excerpt: "…quedamos atentos al calendario que nos compartirá para las 24 unidades de Torre B, idealmente antes del viernes, para reservar las salas de firma de la próxima quincena.",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_pago_estim7",
      kind: "REQUEST_TO_ME",
      title: "Autorizar pago de la estimación 7 a la constructora",
      detail: "Piden visto bueno para liberar el pago de la estimación 7 (avance de obra 62%).",
      counterpart: "Carlos Ortega <cortega@constructora-norte.example.mx>",
      due: dueIn(0),
      priority: "CRITICAL",
      source: {
        message_id: "m2",
        subject: "Estimación 7 — solicitud de autorización de pago",
        sender: "Carlos Ortega <cortega@constructora-norte.example.mx>",
        received_at: iso(-20 * HOUR),
        excerpt: "Adjunto la estimación 7 con el soporte de avance. ¿Nos puede autorizar el pago hoy para no detener el colado de la losa del nivel 9?",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_dash_cobranza",
      kind: "MY_COMMITMENT",
      title: "Compartir el dashboard semanal de cobranza con Dirección",
      detail: "Prometí enviar la versión con cartera vencida por torre y proyección del mes.",
      counterpart: "Dirección General <direccion@empresa.example.mx>",
      due: dueIn(1),
      priority: "MEDIUM",
      source: {
        message_id: "m3",
        subject: "Dashboard de cobranza — versión semanal",
        sender: "Yo <usuario@empresa.example.mx>",
        received_at: iso(-2 * DAY),
        excerpt: "Mañana les comparto el dashboard con la cartera vencida desglosada por torre y la proyección de cobranza de septiembre.",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_regimen_v3",
      kind: "REQUEST_TO_ME",
      title: "Revisar el reglamento del régimen de condominio (v3)",
      detail: "El despacho envió la versión 3 y pide comentarios sobre cuotas de mantenimiento y áreas comunes.",
      counterpart: "Laura Méndez <lmendez@despacho-juridico.example.mx>",
      due: dueIn(3),
      priority: "HIGH",
      source: {
        message_id: "m4",
        subject: "Régimen de propiedad en condominio — reglamento v3",
        sender: "Laura Méndez <lmendez@despacho-juridico.example.mx>",
        received_at: iso(-DAY),
        excerpt: "Le comparto la v3 del reglamento. Necesitamos sus comentarios sobre el esquema de cuotas y el uso del roof garden antes de ingresarlo al registro.",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_avaluo_lote",
      kind: "THEIR_COMMITMENT",
      title: "Enviar el avalúo actualizado del lote",
      detail: "La valuadora quedó de entregar el avalúo actualizado; ya pasó la fecha.",
      counterpart: "Valuadora Delta <avaluos@valuadoradelta.example.mx>",
      due: dueIn(-4),
      status: "WAITING",
      priority: "HIGH",
      draft_id: "drf_avaluo",
      source: {
        message_id: "m5",
        subject: "RE: Avalúo comercial — lote Zapopan",
        sender: "Valuadora Delta <avaluos@valuadoradelta.example.mx>",
        received_at: iso(-9 * DAY),
        excerpt: "Con gusto; le enviamos el avalúo actualizado a más tardar el viernes 19.",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_firma_banco",
      kind: "THEIR_COMMITMENT",
      title: "Confirmar fecha de firma del crédito puente",
      detail: "El banco confirmará la fecha de firma una vez que el comité apruebe la línea.",
      counterpart: "Ana Torres <atorres@banco.example.mx>",
      due: dueIn(5),
      priority: "MEDIUM",
      source: {
        message_id: "m6",
        subject: "Crédito puente — siguiente paso",
        sender: "Ana Torres <atorres@banco.example.mx>",
        received_at: iso(-3 * DAY),
        excerpt: "El comité sesiona el martes; en cuanto tengamos la resolución le confirmo la fecha de firma.",
        web_link: null,
      },
    }),
    fu({
      id: "fup_penalizacion",
      kind: "AWAITING_REPLY",
      title: "Postura sobre penalización por pagos tardíos de clientes",
      detail: "Pregunté si aplicamos la penalización del contrato a 3 clientes con atraso; sin respuesta en 5 días.",
      counterpart: "Roberto Salas <rsalas@empresa.example.mx>",
      due: null,
      status: "WAITING",
      priority: "MEDIUM",
      draft_id: "drf_penalizacion",
      source: {
        message_id: "m7",
        subject: "Pagos tardíos — ¿aplicamos penalización?",
        sender: "Yo <usuario@empresa.example.mx>",
        received_at: iso(-5 * DAY),
        excerpt: "Roberto, ¿aplicamos la penalización de la cláusula 8 a los tres clientes con más de 60 días de atraso, o preferimos proponer convenio?",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_mtto_areas",
      kind: "AWAITING_REPLY",
      title: "Cotización de mantenimiento de áreas comunes",
      detail: "Pedí cotización anual para jardinería, alberca y elevadores.",
      counterpart: "Servicios Integrales <ventas@serviciosintegrales.example.mx>",
      due: dueIn(2),
      priority: "LOW",
      source: {
        message_id: "m8",
        subject: "Solicitud de cotización — mantenimiento áreas comunes",
        sender: "Yo <usuario@empresa.example.mx>",
        received_at: iso(-2 * DAY),
        excerpt: "¿Nos podrían cotizar el servicio anual de mantenimiento de jardines, alberca y elevadores para 2 torres?",
        web_link: OUTLOOK,
      },
    }),
    fu({
      id: "fup_planos_ok",
      kind: "THEIR_COMMITMENT",
      title: "Enviar planos arquitectónicos firmados",
      detail: "Recibidos.",
      counterpart: "Estudio Arquitectura <proyecto@estudio.example.mx>",
      due: dueIn(-6),
      status: "DONE",
      priority: "LOW",
      source: null,
    }),
  ];

  const drafts: EmailDraft[] = [
    {
      id: "drf_avaluo",
      node: NODE,
      followup_id: "fup_avaluo_lote",
      to: ["avaluos@valuadoradelta.example.mx"],
      cc: [],
      subject: "RE: Avalúo comercial — lote Zapopan",
      body:
        "Buen día,\n\nDamos seguimiento al avalúo actualizado del lote en Zapopan que quedaron de enviarnos el viernes 19. " +
        "¿Nos pueden confirmar la nueva fecha de entrega? Lo necesitamos para cerrar el expediente del crédito puente.\n\nQuedo atento, saludos.",
      in_reply_to: "m5",
      status: "PROPOSED",
      export: null,
      download_url: null,
      created_at: iso(-3 * HOUR),
      updated_at: iso(-3 * HOUR),
    },
    {
      id: "drf_penalizacion",
      node: NODE,
      followup_id: "fup_penalizacion",
      to: ["rsalas@empresa.example.mx"],
      cc: [],
      subject: "RE: Pagos tardíos — ¿aplicamos penalización?",
      body:
        "Roberto, retomo este tema: tenemos tres clientes con más de 60 días de atraso. " +
        "¿Aplicamos la penalización de la cláusula 8 o proponemos convenio? Me gustaría definirlo esta semana para avisar a cobranza.\n\nGracias.",
      in_reply_to: "m7",
      status: "PROPOSED",
      export: null,
      download_url: null,
      created_at: iso(-2 * HOUR),
      updated_at: iso(-2 * HOUR),
    },
  ];
  return { followups, drafts };
}

function nextSlot(): string {
  const d = new Date();
  for (const h of [8, 15]) {
    const t = new Date(d);
    t.setHours(h, 0, 0, 0);
    if (t > d) return t.toISOString();
  }
  const t = new Date(d);
  t.setDate(t.getDate() + 1);
  t.setHours(8, 0, 0, 0);
  return t.toISOString();
}

function eml(d: EmailDraft): string {
  return [
    "X-Unsent: 1",
    `To: ${d.to.join(", ")}`,
    d.cc.length ? `Cc: ${d.cc.join(", ")}` : "",
    `Subject: ${d.subject}`,
    "Content-Type: text/plain; charset=utf-8",
    "",
    d.body,
  ]
    .filter((l, i) => l !== "" || i > 2)
    .join("\r\n");
}

export interface MockInbox {
  followups: FollowUp[];
  drafts: EmailDraft[];
  digests: Digest[];
  api: InboxApi;
}

export function mockInbox(emit: Emit, speed: number): MockInbox {
  const mode = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("inbox") : null;
  const s = seed();
  s.followups.push(digestFollowup());
  // ?digest=none starts without a digest (empty state; "Scan now" still produces one).
  const noDigest = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("digest") === "none";
  const digests: Digest[] = noDigest ? [] : [seedDigest()];
  const followups = new Map(s.followups.map((f) => [f.id, f]));
  const drafts = new Map(s.drafts.map((d) => [d.id, d]));
  const later = (ms: number, fn: () => void) => setTimeout(fn, ms / speed);

  let status: InboxStatus = {
    source: mode === "unconfigured" ? null : "graph",
    connected: mode !== "disconnected" && mode !== "unconfigured",
    account: mode === "disconnected" || mode === "unconfigured" ? null : "usuario@empresa.example.mx",
    last_scan: iso(-2 * HOUR),
    next_scan: nextSlot(),
    schedule: "08:00,15:00",
    processed_count: 214,
    hint:
      mode === "unconfigured"
        ? "Set ATLAS_MS_CLIENT_ID (Microsoft 365) or ATLAS_MAIL_FOLDER (Power Automate folder) in apps/api/.env, then restart ATLAS."
        : mode === "disconnected"
          ? "Sign in to Microsoft 365 to let ATLAS read your mailbox."
          : null,
  };
  let connectStarted = 0;

  const putFollowup = (f: FollowUp, summary: string, agent: string | null = "hermes") => {
    followups.set(f.id, f);
    emit("followup.upserted", { followup: f }, summary, agent);
  };
  const putDraft = (d: EmailDraft, summary: string, agent: string | null = "alfred") => {
    drafts.set(d.id, d);
    emit("draft.upserted", { draft: d }, summary, agent);
  };

  const api: InboxApi = {
    async status() {
      if (mode === "none") return null;
      return { ...status };
    },
    async scan() {
      if (!status.connected) throw new Error("ATLAS API 409: inbox not connected");
      emit("log", {}, "HERMES started an inbox scan", "hermes");
      later(1800, () => {
        const f = fu({
          id: rid("fup"),
          kind: "REQUEST_TO_ME",
          title: "Enviar la minuta de la junta de obra del lunes",
          detail: "Piden la minuta con acuerdos y responsables para circularla con los contratistas.",
          counterpart: "Ing. Patricia Vega <pvega@supervision.example.mx>",
          due: dueIn(2),
          priority: "MEDIUM",
          created_at: iso(0),
          updated_at: iso(0),
          source: {
            message_id: rid("msg"),
            subject: "Minuta junta de obra — acuerdos",
            sender: "Ing. Patricia Vega <pvega@supervision.example.mx>",
            received_at: iso(-40 * 60_000),
            excerpt: "¿Nos puede enviar la minuta con los acuerdos y responsables a más tardar el viernes? La circulamos con los contratistas.",
            web_link: OUTLOOK,
          },
        });
        putFollowup(f, `HERMES found a follow-up: ${f.title}`);
        const dig = scanDigest(status.last_scan);
        digests.push(dig);
        emit("digest.ready", { digest: dig }, `HERMES published a CC digest — ${dig.threads.length} threads`, "hermes");
        status = { ...status, last_scan: iso(0), next_scan: nextSlot(), processed_count: status.processed_count + 7 };
        emit("log", {}, "Inbox scan complete — 7 emails read, 1 new follow-up", "hermes");
      });
      return { ok: true };
    },
    async runDigest(days: number) {
      if (!status.connected) throw new Error('ATLAS API 409: {"detail":"No mailbox is connected. Connect your inbox first."}');
      if (!Number.isInteger(days) || days < 1 || days > 14) throw new Error('ATLAS API 422: {"detail":"days must be between 1 and 14"}');
      const id = rid("msn");
      const base: Mission = {
        id,
        objective: `CC digest · last ${days} day${days === 1 ? "" : "s"}`,
        node: "corporate",
        mode: "live",
        attachments: [],
        round: 1,
        interrupted: false,
        usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, llm_calls: 0, est_cost_usd: 0 },
        usage_by_agent: {},
        context: "Inbox",
        phase: "EXECUTION",
        priority: "MEDIUM",
        task_ids: [],
        final_report_id: null,
        created_at: iso(0),
        closed_at: null,
      };
      emit("mission.created", { mission: base }, `Mission received: ${base.objective}`, "hermes", id);
      emit("log", {}, `HERMES is reading CC emails from the last ${days} day${days === 1 ? "" : "s"}`, "hermes", id);
      later(3200, () => {
        // days=1 → no candidates (exercises the "finished without a digest" path).
        const empty = days === 1;
        let summary: string;
        if (empty) {
          summary = "No CC emails in the last 1 day — nothing to digest (3 automated notices skipped).";
        } else {
          const dig = { ...seedDigest(), id: rid("dig"), window_start: iso(-days * DAY), window_end: iso(0), created_at: iso(0), mission_id: id };
          digests.push(dig);
          emit("digest.ready", { digest: dig }, `HERMES published a CC digest — ${dig.threads.length} threads`, "hermes", id);
          summary = `CC digest: ${dig.threads.length} threads from the last ${days} days.`;
        }
        const report: MissionReport = {
          id: rid("rpt"),
          mission_id: id,
          executive_summary: summary,
          objective_status: empty ? "NOT_ACHIEVED" : "ACHIEVED",
          tasks_completed: [],
          tasks_pending: [],
          key_findings: [],
          conflicts: [],
          assumptions: [],
          needs_human_attention: [],
          next_actions: [],
          references: [],
          agent_report_ids: [],
          version: 1,
          deliverables: [],
          audit_summary: "",
          untraced: [],
          created_at: iso(0),
        };
        emit("log", {}, summary, "hermes", id);
        emit("mission.report_ready", { report }, "HERMES published the mission report", "hermes", id);
        emit("mission.closed", { mission: { ...base, phase: "CLOSED", final_report_id: report.id, closed_at: iso(0) } }, "Mission closed", "hermes", id);
      });
      return base;
    },
    async connect(): Promise<InboxConnectStart> {
      connectStarted = Date.now();
      return {
        user_code: "HX7-QK4P",
        verification_uri: "https://microsoft.com/devicelogin",
        expires_in: 900,
        message: "To sign in, open https://microsoft.com/devicelogin and enter the code HX7-QK4P to authenticate.",
      };
    },
    async connectStatus(): Promise<InboxConnectState> {
      // ?inbox=disconnected&hold=1 keeps the flow pending (for screenshots).
      const hold = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("hold") === "1";
      if (!hold && connectStarted && Date.now() - connectStarted > 8000 / speed) {
        status = { ...status, source: "graph", connected: true, account: "usuario@empresa.example.mx", hint: null };
        return { state: "connected", account: status.account, error: null };
      }
      return { state: "pending", account: null, error: null };
    },
    async patchFollowup(id: string, patch: FollowUpPatch) {
      const f = followups.get(id);
      if (!f) throw new Error("ATLAS API 404: follow-up not found");
      const next: FollowUp = { ...f, ...patch, updated_at: iso(0) };
      const what = patch.status ? `marked ${patch.status.toLowerCase()}` : patch.due !== undefined ? "snoozed" : "updated";
      putFollowup(next, `Follow-up ${what}: ${f.title}`, null);
      return next;
    },
    async draftFollowup(id: string) {
      const f = followups.get(id);
      if (!f) throw new Error("ATLAS API 404: follow-up not found");
      const existing = f.draft_id ? drafts.get(f.draft_id) : undefined;
      if (existing?.status === "PROPOSED") return existing;
      later(1400, () => {
        const addr = f.counterpart?.match(/<([^>]+)>/)?.[1] ?? f.counterpart ?? "";
        const name = f.counterpart?.split("<")[0].trim() ?? "";
        // Greet people by first name; organizations ("Notaría 14 — …") get a neutral greeting.
        const first = name && !/—|\d/.test(name) && name.split(" ").length <= 3 ? name.split(" ")[0] : "";
        const d: EmailDraft = {
          id: rid("drf"),
          node: NODE,
          followup_id: f.id,
          to: addr ? [addr] : [],
          cc: [],
          subject: f.source ? (f.source.subject.startsWith("RE:") ? f.source.subject : `RE: ${f.source.subject}`) : f.title,
          body: `${first ? `Hola ${first},` : "Buen día,"}\n\nDoy seguimiento a: ${f.title.charAt(0).toLowerCase()}${f.title.slice(1)}. ¿Me confirmas cómo vamos?\n\nSaludos.`,
          in_reply_to: f.source?.message_id ?? null,
          status: "PROPOSED",
          export: null,
          download_url: null,
          created_at: iso(0),
          updated_at: iso(0),
        };
        putDraft(d, `ALFRED drafted a follow-up: ${d.subject}`);
        putFollowup({ ...followups.get(f.id)!, draft_id: d.id, updated_at: iso(0) }, `Draft attached to follow-up: ${f.title}`, "alfred");
      });
      return { ok: true };
    },
    async decideDraft(id: string, body: DraftDecisionBody) {
      const d = drafts.get(id);
      if (!d) throw new Error("ATLAS API 404: draft not found");
      if (body.decision === "DISCARDED") {
        const next: EmailDraft = { ...d, status: "DISCARDED", updated_at: iso(0) };
        putDraft(next, `Draft discarded: ${d.subject}`, null);
        return next;
      }
      const edited: EmailDraft = {
        ...d,
        to: body.to ?? d.to,
        cc: body.cc ?? d.cc,
        subject: body.subject ?? d.subject,
        body: body.body ?? d.body,
      };
      const url = typeof URL !== "undefined" && typeof Blob !== "undefined" ? URL.createObjectURL(new Blob([eml(edited)], { type: "message/rfc822" })) : null;
      const next: EmailDraft = { ...edited, status: "EXPORTED", export: "eml", download_url: url, updated_at: iso(0) };
      putDraft(next, `Draft approved and exported: ${d.subject}`, null);
      return next;
    },
  };

  return { followups: s.followups, drafts: s.drafts, digests, api };
}
