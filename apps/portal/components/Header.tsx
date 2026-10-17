"use client";

import { useEffect, useState } from "react";
import { STREAM_API_URL } from "@/lib/api";
import { cx } from "@/lib/format";
import type { StreamStatus } from "@/lib/useAlerts";
import { useAlerts } from "./AlertsProvider";

const STATUS_META: Record<
  StreamStatus,
  { label: string; pill: string; dot: string }
> = {
  live: {
    label: "live feed",
    pill: "border-emerald-600/40 bg-emerald-950/60 text-emerald-300",
    dot: "bg-emerald-400 animate-pulse",
  },
  connecting: {
    label: "connecting",
    pill: "border-amber-600/40 bg-amber-950/60 text-amber-300",
    dot: "bg-amber-400 animate-pulse",
  },
  offline: {
    label: "stream offline",
    pill: "border-red-600/40 bg-red-950/60 text-red-300",
    dot: "bg-red-500",
  },
};

/** Platform chrome: identity, stream-api endpoint, WS status dot, clock. */
export function Header() {
  const { status, alerts } = useAlerts();
  const [now, setNow] = useState<Date | null>(null);

  useEffect(() => {
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const meta = STATUS_META[status];

  return (
    <header className="sticky top-0 z-20 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
      <div className="mx-auto flex h-12 w-full max-w-[1700px] items-center gap-3 px-4">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded bg-red-600 text-sm font-bold text-white shadow-[0_0_14px_rgba(220,38,38,0.45)]">
          ₹
        </div>
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="text-sm font-semibold tracking-[0.16em] text-zinc-100">
            UPI&nbsp;FRAUD
          </span>
          <span className="hidden text-[10px] uppercase tracking-[0.3em] text-zinc-500 sm:inline">
            soc console
          </span>
        </div>
        <span
          className="hidden max-w-[280px] truncate font-mono text-[10px] text-zinc-600 lg:inline"
          title={STREAM_API_URL}
        >
          {STREAM_API_URL}
        </span>

        <div className="ml-auto flex items-center gap-3">
          <span className="hidden font-mono text-[11px] tabular-nums text-zinc-500 sm:inline">
            {now ? now.toLocaleTimeString("en-GB", { hour12: false }) : "--:--:--"}
            <span className="ml-1 text-[9px] text-zinc-600">LOCAL</span>
          </span>
          <span
            className={cx(
              "flex items-center gap-1.5 rounded border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]",
              meta.pill,
            )}
            title={`WebSocket ${STREAM_API_URL.replace(/^http/, "ws")}/ws/alerts — ${alerts.length} alert(s) buffered`}
            data-status={status}
          >
            <span className={cx("h-1.5 w-1.5 rounded-full", meta.dot)} />
            <span>{meta.label}</span>
            <span className="rounded-sm bg-black/30 px-1 font-mono text-[10px] tabular-nums opacity-80">
              {alerts.length}
            </span>
          </span>
        </div>
      </div>
    </header>
  );
}
