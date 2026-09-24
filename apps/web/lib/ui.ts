"use client";

import { useEffect, useState } from "react";
import type { AgentMessage, AgentStatus, ClaimKind, MessageType, MissionPhase, Priority, TaskStatus } from "./contracts";

export const STATUS: Record<AgentStatus, { color: string; label: string; active: boolean }> = {
  WORKING: { color: "#22c55e", label: "Working", active: true },
  WAITING: { color: "#eab308", label: "Waiting", active: true },
  COLLABORATING: { color: "#3b82f6", label: "Collaborating", active: true },
  REVIEWING: { color: "#a855f7", label: "Reviewing", active: true },
  MONITORING: { color: "#22d3ee", label: "Monitoring", active: true },
  BLOCKED: { color: "#ef4444", label: "Blocked", active: true },
  COMPLETED: { color: "#cbd5e1", label: "Completed", active: false },
  ERROR: { color: "#f43f5e", label: "Error", active: true },
  IDLE: { color: "#475569", label: "Idle", active: false },
};

/** Priority for aggregating a group (division) into one status. */
const STATUS_RANK: AgentStatus[] = ["ERROR", "BLOCKED", "WORKING", "COLLABORATING", "REVIEWING", "WAITING", "MONITORING", "COMPLETED", "IDLE"];
export function aggregateStatus(list: AgentStatus[]): AgentStatus {
  for (const s of STATUS_RANK) if (list.includes(s)) return s;
  return "IDLE";
}

export const TASK_STATUS: Record<TaskStatus, { color: string; label: string }> = {
  PENDING: { color: "#64748b", label: "Pending" },
  READY: { color: "#94a3b8", label: "Ready" },
  IN_PROGRESS: { color: "#22c55e", label: "Running" },
  AWAITING_APPROVAL: { color: "#f59e0b", label: "Approval" },
  BLOCKED: { color: "#ef4444", label: "Blocked" },
  IN_REVIEW: { color: "#a855f7", label: "In review" },
  COMPLETED: { color: "#cbd5e1", label: "Done" },
  FAILED: { color: "#f43f5e", label: "Failed" },
  CANCELLED: { color: "#475569", label: "Cancelled" },
};

export const PRIORITY: Record<Priority, { color: string; label: string }> = {
  LOW: { color: "#64748b", label: "LOW" },
  MEDIUM: { color: "#7dd3fc", label: "MED" },
  HIGH: { color: "#fbbf24", label: "HIGH" },
  CRITICAL: { color: "#f87171", label: "CRIT" },
};

export const MSG: Record<MessageType, { color: string; label: string }> = {
  REQUEST: { color: "#38bdf8", label: "Request" },
  RESULT: { color: "#22c55e", label: "Result" },
  ALERT: { color: "#ef4444", label: "Alert" },
  QUESTION: { color: "#eab308", label: "Question" },
  ANSWER: { color: "#2dd4bf", label: "Answer" },
  REVIEW: { color: "#a855f7", label: "Review" },
};

export const CLAIM: Record<ClaimKind, { color: string; label: string; hint: string }> = {
  FACT: { color: "#38bdf8", label: "Fact", hint: "Verified with sources" },
  ASSUMPTION: { color: "#eab308", label: "Assumption", hint: "Not verified — treat with care" },
  SCENARIO: { color: "#a78bfa", label: "Scenario", hint: "Conditional projection" },
  RECOMMENDATION: { color: "#22c55e", label: "Recommendation", hint: "Proposed course of action" },
};

export const PHASES: MissionPhase[] = [
  "OBJECTIVE",
  "DECOMPOSITION",
  "DELEGATION",
  "EXECUTION",
  "COLLABORATION",
  "VALIDATION",
  "CONSOLIDATION",
  "REPORTING",
  "FOLLOW_UP",
];

export const REASON_LABEL: Record<string, string> = {
  EXTERNAL_COMMUNICATION: "External communication",
  FINANCIAL_COMMITMENT: "Financial commitment",
  CONSEQUENTIAL_DECISION: "Consequential decision",
  AMBIGUOUS_OR_CONFLICTING: "Needs clarification",
  IRREVERSIBLE_ACTION: "Irreversible action",
  INSUFFICIENT_INFORMATION: "Needs information",
};

/** Approval reasons that are really questions to the human — the note is the answer. */
export const QUESTION_REASONS = new Set(["INSUFFICIENT_INFORMATION", "AMBIGUOUS_OR_CONFLICTING"]);

export const reasonLabel = (r: string) => REASON_LABEL[r] ?? human(r).toLowerCase().replace(/^./, (c) => c.toUpperCase());

export const human = (s: string) => s.replace(/_/g, " ");

export function hms(iso: string | number | Date): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

export function elapsed(fromIso: string, to: number): string {
  const s = Math.max(0, Math.floor((to - new Date(fromIso).getTime()) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const p = (n: number) => n.toString().padStart(2, "0");
  return h ? `${p(h)}:${p(m)}:${p(sec)}` : `${p(m)}:${p(sec)}`;
}

/** Progress arrives as 0..1 from the API; tolerate 0..100 defensively. */
export const pct = (p: number) => Math.round(Math.max(0, Math.min(1, p > 1 ? p / 100 : p)) * 100);

/** The API aliases from/to; tolerate from_agent/to_agent if a serializer skips aliases. */
export const msgFrom = (m: AgentMessage) => m.from ?? (m as unknown as { from_agent?: string }).from_agent ?? "";
export const msgTo = (m: AgentMessage) => m.to ?? (m as unknown as { to_agent?: string }).to_agent ?? "";

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

/** 1234 → "1.2k", 48200 → "48k", 2.1e6 → "2.10M" */
export const fmtTokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
export const fmtUsd = (n: number) => (n > 0 && n < 0.01 ? "<$0.01" : `$${n.toFixed(2)}`);
