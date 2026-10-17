import type { DecisionAction } from "./types";

/** Join conditional class names (falsy parts dropped). */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Visual tokens for each decision action (badge/bar/text colours). */
export const ACTION_META: Record<
  DecisionAction,
  { badge: string; bar: string; text: string; dot: string; hex: string }
> = {
  ALLOW: {
    badge: "border-emerald-600/40 bg-emerald-950/60 text-emerald-400",
    bar: "bg-emerald-500",
    text: "text-emerald-400",
    dot: "bg-emerald-400",
    hex: "#34d399",
  },
  CHALLENGE: {
    badge: "border-amber-600/40 bg-amber-950/60 text-amber-400",
    bar: "bg-amber-500",
    text: "text-amber-400",
    dot: "bg-amber-400",
    hex: "#fbbf24",
  },
  BLOCK: {
    badge: "border-red-600/40 bg-red-950/60 text-red-400",
    bar: "bg-red-500",
    text: "text-red-400",
    dot: "bg-red-400",
    hex: "#f87171",
  },
};

/** `1234567.5` → `1,23,456.75` (Indian digit grouping). */
export function formatInr(amount: number): string {
  if (!Number.isFinite(amount)) return "—";
  return new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 0,
  }).format(amount);
}

/** UUID → first 8 chars, enough to correlate with logs. */
export function shortTx(txId: string): string {
  return txId.length > 12 ? `${txId.slice(0, 8)}…` : txId;
}

/** `very.long.payer@okhdfcbank` → `very.long.pa…` (keeps tables dense). */
export function shortVpa(vpa: string, max = 20): string {
  return vpa.length > max ? `${vpa.slice(0, max - 1)}…` : vpa;
}

/** Epoch ms → `HH:MM:SS` in the viewer's locale (24 h). */
export function formatTime(timestampMs: number): string {
  if (!Number.isFinite(timestampMs)) return "—";
  return new Date(timestampMs).toLocaleTimeString("en-GB", { hour12: false });
}

/** Epoch ms → `16 Oct 2026, 14:03:22` for tooltips. */
export function formatDateTime(timestampMs: number): string {
  if (!Number.isFinite(timestampMs)) return "—";
  return new Date(timestampMs).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** Seconds/minutes/hours since `timestampMs`, compact (`12s`, `4m`, `3h`). */
export function timeAgo(timestampMs: number, nowMs: number = Date.now()): string {
  const deltaSec = Math.max(0, Math.round((nowMs - timestampMs) / 1000));
  if (deltaSec < 60) return `${deltaSec}s`;
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}m`;
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}h`;
  return `${Math.floor(deltaSec / 86400)}d`;
}

/** Clamp a 0..1 score to a 0..100 % bar width. */
export function scorePercent(score: number): number {
  if (!Number.isFinite(score)) return 0;
  return Math.round(Math.min(1, Math.max(0, score)) * 100);
}
