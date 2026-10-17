"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, fetchStats, STREAM_API_URL } from "@/lib/api";
import { cx } from "@/lib/format";
import type { DecisionStats } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

const REFRESH_MS = 10_000;

const CARDS = [
  { key: "ALLOW", dot: "bg-emerald-400", text: "text-emerald-400" },
  { key: "CHALLENGE", dot: "bg-amber-400", text: "text-amber-400" },
  { key: "BLOCK", dot: "bg-red-400", text: "text-red-400" },
  { key: "TOTAL", dot: "bg-sky-400", text: "text-sky-300" },
] as const;

function statValue(stats: DecisionStats | null, key: (typeof CARDS)[number]["key"]): number | null {
  if (!stats) return null;
  if (key === "TOTAL") return stats.ALLOW + stats.CHALLENGE + stats.BLOCK;
  return stats[key];
}

/** Decision-count tiles from `GET /v1/stats`, auto-refreshed every 10 s. */
export function StatCards() {
  const [stats, setStats] = useState<DecisionStats | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const next = await fetchStats();
      if (seq !== requestSeq.current) return;
      setStats(next);
      setError("");
      setState("ready");
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(err));
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const total = statValue(stats, "TOTAL");

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {CARDS.map((card) => {
          const value = statValue(stats, card.key);
          const share =
            card.key !== "TOTAL" && total !== null && total > 0 && value !== null
              ? `${Math.round((value / total) * 100)}%`
              : null;
          return (
            <div
              key={card.key}
              className="relative min-w-0 rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">
                  <span className={cx("h-1.5 w-1.5 rounded-full", card.dot)} />
                  {card.key}
                </span>
                {share ? (
                  <span className="font-mono text-[10px] tabular-nums text-zinc-600">
                    {share}
                  </span>
                ) : null}
              </div>
              <div
                className={cx(
                  "mt-1 truncate text-2xl font-semibold tabular-nums",
                  state === "ready" && value !== null
                    ? card.text
                    : "text-zinc-700",
                )}
              >
                {state === "ready" && value !== null
                  ? value.toLocaleString("en-IN")
                  : "—"}
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        {state === "error" ? (
          <>
            <span className="rounded border border-red-600/40 bg-red-950/60 px-1.5 py-0.5 font-semibold uppercase tracking-wider text-red-400">
              stream offline
            </span>
            <span className="truncate font-mono text-zinc-500" title={error}>
              {error}
            </span>
          </>
        ) : (
          <span className="font-mono text-zinc-600">
            {state === "loading"
              ? `fetching ${STREAM_API_URL}/v1/stats …`
              : `stats auto-refresh ${REFRESH_MS / 1_000}s · ${STREAM_API_URL}`}
          </span>
        )}
      </div>
    </div>
  );
}
