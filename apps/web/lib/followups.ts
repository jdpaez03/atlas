import type { EmailDraft, FollowUp } from "./contracts";

export type Column = "mine" | "theirs" | "replies";

export const COLUMNS: { id: Column; title: string; hint: string; kinds: FollowUp["kind"][]; color: string }[] = [
  { id: "mine", title: "Mine to do", hint: "What I committed to or was asked for", kinds: ["MY_COMMITMENT", "REQUEST_TO_ME"], color: "#7dd3fc" },
  { id: "theirs", title: "Waiting on others", hint: "What others committed to me", kinds: ["THEIR_COMMITMENT"], color: "#fbbf24" },
  { id: "replies", title: "Replies owed to me", hint: "Emails I sent with no answer yet", kinds: ["AWAITING_REPLY"], color: "#f0abfc" },
];

export const KIND_LABEL: Record<FollowUp["kind"], string> = {
  MY_COMMITMENT: "I committed",
  REQUEST_TO_ME: "Asked of me",
  THEIR_COMMITMENT: "They committed",
  AWAITING_REPLY: "No answer",
};

export const isOpen = (f: FollowUp) => f.status === "OPEN" || f.status === "WAITING";

/** Whole calendar days from today (local) to `iso`; negative = in the past. */
/** Parse a due date: "yyyy-mm-dd" is a local calendar date (new Date() would read it as UTC midnight,
 *  i.e. the previous evening in Mexico). Full date-times parse normally. */
export function parseDue(iso: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(iso);
}

export function dayDiff(iso: string, now: number): number {
  const d = parseDue(iso);
  const a = new Date(now);
  const da = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const db = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate());
  return Math.round((da - db) / 86_400_000);
}

export function isOverdue(f: FollowUp, now: number): boolean {
  return !!f.due && isOpen(f) && dayDiff(f.due, now) < 0;
}

export function dueLabel(due: string, now: number): { text: string; tone: "late" | "today" | "soon" | "later" } {
  const n = dayDiff(due, now);
  if (n < 0) return { text: n === -1 ? "1 day late" : `${-n} days late`, tone: "late" };
  if (n === 0) return { text: "due today", tone: "today" };
  if (n === 1) return { text: "due tomorrow", tone: "soon" };
  return { text: `in ${n} days`, tone: n <= 3 ? "soon" : "later" };
}

/** "in 3h", "2h ago", "in 12 min" */
export function relTime(iso: string | null | undefined, now: number): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return "—";
  const abs = Math.abs(ms);
  const m = Math.round(abs / 60_000);
  const txt = m < 1 ? "now" : m < 60 ? `${m} min` : m < 60 * 24 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
  if (txt === "now") return "just now";
  return ms > 0 ? `in ${txt}` : `${txt} ago`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("es-MX", { day: "numeric", month: "short" });
}

/** "Name <email>" → { name, email } */
export function parseAddr(s: string | null | undefined): { name: string; email: string | null } {
  if (!s) return { name: "—", email: null };
  const m = s.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  if (m) return { name: m[1].trim() || m[2], email: m[2] };
  return { name: s.trim(), email: s.includes("@") ? s.trim() : null };
}

const PRIO_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

/** Overdue first, then by due date (none last), then priority. */
export function sortFollowups(list: FollowUp[]): FollowUp[] {
  return [...list].sort((a, b) => {
    const ao = isOpen(a) ? 0 : 1;
    const bo = isOpen(b) ? 0 : 1;
    if (ao !== bo) return ao - bo;
    const ad = a.due ? parseDue(a.due).getTime() : Infinity;
    const bd = b.due ? parseDue(b.due).getTime() : Infinity;
    if (ad !== bd) return ad - bd;
    return (PRIO_RANK[a.priority] ?? 9) - (PRIO_RANK[b.priority] ?? 9);
  });
}

export function followupCounts(list: FollowUp[], now: number): { open: number; overdue: number } {
  let open = 0;
  let overdue = 0;
  for (const f of list) {
    if (!isOpen(f)) continue;
    open++;
    if (now && isOverdue(f, now)) overdue++;
  }
  return { open, overdue };
}

export const proposedDrafts = (drafts: EmailDraft[]) => drafts.filter((d) => d.status === "PROPOSED");

/** yyyy-mm-dd for <input type=date>, local. */
export function toDateInput(d: Date): string {
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** FollowUp.due is a calendar date on the API (yyyy-mm-dd). Sending a date-time would be rejected. */
export function dateInputToIso(v: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : v.slice(0, 10);
}
