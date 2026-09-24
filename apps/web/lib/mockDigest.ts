/**
 * Mock CC digests (docs/INBOX.md §4): one seeded digest (5 threads, 3 projects + untagged, one "asks me" linked
 * to a follow-up) and a smaller one produced by "Scan now". Digest EmailRefs carry no excerpt, as in the engine.
 */
import type { Digest, DigestThread, EmailRef, FollowUp } from "./contracts";

const NODE = "corporate";
const HOUR = 3600_000;
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();
const OUTLOOK = "https://outlook.office.com/mail/";

let n = 0;
const rid = (p: string) => `${p}_${Date.now().toString(36)}${(n++).toString(36)}`;

const ref = (subject: string, sender: string, hoursAgo: number, link = true): EmailRef => ({
  message_id: rid("msg"),
  subject,
  sender,
  received_at: iso(-hoursAgo * HOUR),
  excerpt: "",
  web_link: link ? OUTLOOK : null,
});

/** The follow-up HERMES created from the digest thread that asks the user something. */
export function digestFollowup(): FollowUp {
  const d = new Date();
  d.setDate(d.getDate() + 2);
  const p = (x: number) => x.toString().padStart(2, "0");
  return {
    id: "fup_digest_comite",
    node: NODE,
    kind: "REQUEST_TO_ME",
    title: "Enviar al banco el reporte de ventas de agosto para el comité",
    detail: "En el hilo del crédito puente (en CC) Ana Torres pide el reporte de ventas de agosto antes del comité.",
    counterpart: "Ana Torres <atorres@banco.example.mx>",
    due: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
    status: "OPEN",
    priority: "HIGH",
    source: ref("RE: Crédito puente Torre Norte — documentación para comité", "Ana Torres <atorres@banco.example.mx>", 5),
    draft_id: null,
    mission_id: null,
    created_at: iso(-2 * HOUR),
    updated_at: iso(-2 * HOUR),
  };
}

export function seedDigest(): Digest {
  const threads: DigestThread[] = [
    {
      conversation_id: "conv_credito",
      subject: "RE: Crédito puente Torre Norte — documentación para comité",
      project: "Torre Norte",
      participants: ["Ana Torres <atorres@banco.example.mx>", "Dirección Finanzas <finanzas@empresa.example.mx>", "Jorge Luna <jluna@empresa.example.mx>"],
      summary: [
        "El banco confirmó que el comité de crédito sesiona el martes.",
        "Finanzas ya envió estados financieros y el avance de obra certificado.",
        "Falta el reporte de ventas de agosto con unidades escrituradas y apartadas.",
        "El banco sugiere presentar el reporte con el formato de su plantilla.",
      ],
      decisions: ["Se presentará la solicitud con la línea de MXN 180M (no la ampliación a 210M)."],
      figures: ["Línea solicitada: MXN 180M", "Preventa mínima exigida: 40% de las unidades", "Avance de obra certificado: 62%"],
      asks_me: "Ana Torres pide que le envíes el reporte de ventas de agosto antes del lunes.",
      importance: "HIGH",
      messages: [
        ref("RE: Crédito puente Torre Norte — documentación para comité", "Ana Torres <atorres@banco.example.mx>", 5),
        ref("RE: Crédito puente Torre Norte — documentación para comité", "Dirección Finanzas <finanzas@empresa.example.mx>", 9),
      ],
      followup_id: "fup_digest_comite",
    },
    {
      conversation_id: "conv_escrituras",
      subject: "Escrituración Torre Norte — avance semanal",
      project: "Torre Norte",
      participants: ["Notaría 14 <escrituras@notaria14.example.mx>", "Karla Ríos <krios@empresa.example.mx>"],
      summary: [
        "Se firmaron 9 escrituras esta semana; quedan 15 de la primera etapa.",
        "Dos clientes reprogramaron por retraso en su crédito hipotecario.",
        "La notaría pide los avisos de terminación de obra de los niveles 10 a 12.",
      ],
      decisions: [],
      figures: ["9 escrituras firmadas esta semana", "15 pendientes de la primera etapa"],
      asks_me: null,
      importance: "MEDIUM",
      messages: [ref("Escrituración Torre Norte — avance semanal", "Karla Ríos <krios@empresa.example.mx>", 20)],
      followup_id: null,
    },
    {
      conversation_id: "conv_regimen",
      subject: "Régimen de condominio Residencial Bosque — observaciones del registro",
      project: "Residencial Bosque",
      participants: ["Laura Méndez <lmendez@despacho-juridico.example.mx>", "Jorge Luna <jluna@empresa.example.mx>"],
      summary: [
        "El registro público devolvió el régimen con dos observaciones sobre indivisos.",
        "El despacho corregirá la tabla de indivisos y reingresará el trámite.",
        "El reingreso no requiere nueva firma de la constructora.",
      ],
      decisions: ["Reingresar el régimen el jueves con la tabla corregida."],
      figures: ["2 observaciones del registro", "Tiempo estimado de respuesta: 10 días hábiles"],
      asks_me: null,
      importance: "HIGH",
      messages: [
        ref("Régimen de condominio Residencial Bosque — observaciones del registro", "Laura Méndez <lmendez@despacho-juridico.example.mx>", 7),
        ref("RE: Régimen de condominio Residencial Bosque — observaciones del registro", "Jorge Luna <jluna@empresa.example.mx>", 6, false),
      ],
      followup_id: null,
    },
    {
      conversation_id: "conv_cobranza",
      subject: "Dashboard de cobranza — ajuste de indicadores",
      project: "Administración",
      participants: ["Roberto Salas <rsalas@empresa.example.mx>", "Equipo BI <bi@empresa.example.mx>"],
      summary: [
        "BI propuso separar cartera vencida por antigüedad (30/60/90 días).",
        "Cobranza pidió incluir la proyección semanal junto al acumulado del mes.",
        "La nueva versión se publicará el lunes.",
      ],
      decisions: ["Se adopta el corte por antigüedad 30/60/90 en el dashboard."],
      figures: ["Cartera vencida > 60 días: MXN 3.4M"],
      asks_me: null,
      importance: "MEDIUM",
      messages: [ref("Dashboard de cobranza — ajuste de indicadores", "Equipo BI <bi@empresa.example.mx>", 11)],
      followup_id: null,
    },
    {
      conversation_id: "conv_evento",
      subject: "Convivio de fin de mes — confirmación de asistencia",
      project: null,
      participants: ["Recursos Humanos <rh@empresa.example.mx>"],
      summary: ["RH organiza el convivio del último viernes del mes.", "Piden confirmar asistencia por área antes del miércoles.", "Habrá opción vegetariana."],
      decisions: [],
      figures: [],
      asks_me: null,
      importance: "LOW",
      messages: [ref("Convivio de fin de mes — confirmación de asistencia", "Recursos Humanos <rh@empresa.example.mx>", 14)],
      followup_id: null,
    },
  ];
  return {
    id: "dig_seed",
    node: NODE,
    mission_id: null,
    window_start: iso(-26 * HOUR),
    window_end: iso(-2 * HOUR),
    headline: [
      "Crédito puente Torre Norte: el comité sesiona el martes y el banco te pide el reporte de ventas de agosto.",
      "Régimen de Residencial Bosque devuelto con 2 observaciones; se reingresa el jueves.",
      "Escrituración avanza: 9 firmadas esta semana, 15 pendientes de la primera etapa.",
    ],
    threads,
    skipped: 6,
    created_at: iso(-2 * HOUR),
  };
}

/** The digest produced by a mock "Scan now". */
export function scanDigest(windowStart: string | null): Digest {
  return {
    id: rid("dig"),
    node: NODE,
    mission_id: null,
    window_start: windowStart,
    window_end: iso(0),
    headline: ["Constructora confirma colado de losa del nivel 9 para el sábado si se libera la estimación 7."],
    threads: [
      {
        conversation_id: rid("conv"),
        subject: "Programa de obra Torre Norte — losa nivel 9",
        project: "Torre Norte",
        participants: ["Carlos Ortega <cortega@constructora-norte.example.mx>", "Supervisión <supervision@empresa.example.mx>"],
        summary: [
          "La constructora tiene lista la cimbra del nivel 9.",
          "El colado se programa para el sábado, sujeto al pago de la estimación 7.",
          "Supervisión validó el armado de acero sin observaciones.",
        ],
        decisions: ["Colado del nivel 9 el sábado a las 7:00."],
        figures: ["Volumen de concreto: 86 m³"],
        asks_me: null,
        importance: "HIGH",
        messages: [ref("Programa de obra Torre Norte — losa nivel 9", "Carlos Ortega <cortega@constructora-norte.example.mx>", 0.6)],
        followup_id: null,
      },
      {
        conversation_id: rid("conv"),
        subject: "Proveedores — actualización de lista de precios",
        project: null,
        participants: ["Compras <compras@empresa.example.mx>"],
        summary: ["Compras recibió la nueva lista de precios de acero.", "El ajuste aplica a pedidos a partir del 1 de octubre.", "Se revisarán contratos marco la próxima semana."],
        decisions: [],
        figures: ["Incremento en acero: 4.5%"],
        asks_me: null,
        importance: "MEDIUM",
        messages: [ref("Proveedores — actualización de lista de precios", "Compras <compras@empresa.example.mx>", 1.2)],
        followup_id: null,
      },
    ],
    skipped: 2,
    created_at: iso(0),
  };
}
