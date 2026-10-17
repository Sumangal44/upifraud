"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage, fetchDecisions, STREAM_API_URL } from "@/lib/api";
import {
  ACTION_META,
  cx,
  formatDateTime,
  formatInr,
  formatTime,
  scorePercent,
  shortTx,
  shortVpa,
} from "@/lib/format";
import type { Alert, Decision, DecisionAction } from "@/lib/types";
import { ActionBadge } from "./ActionBadge";
import { Panel } from "./Panel";

type Filter = "ALL" | "BLOCK" | "CHALLENGE";
type LoadState = "loading" | "ready" | "error";

const REFRESH_MS = 10_000;
const LIMIT = 50;

const FILTERS: { id: Filter; label: string }[] = [
  { id: "ALL", label: "All" },
  { id: "BLOCK", label: "Block" },
  { id: "CHALLENGE", label: "Challenge" },
];

function topReason(decision: Decision): string {
  return (
    decision.reasons?.[0] ?? decision.top_factors?.[0]?.name ?? "—"
  );
}

/** Payment context for a decision row: enriched decision fields, else the
 * matching live alert (decisions in the core crate carry no VPA/amount). */
function flowOf(
  decision: Decision,
  alertsByTx: Map<string, Alert>,
): { src: string | null; dst: string | null; amount: number | null } {
  const alert = alertsByTx.get(decision.tx_id);
  const src = decision.src_vpa ?? alert?.src_vpa ?? null;
  const dst = decision.dst_vpa ?? alert?.dst_vpa ?? null;
  const amount = decision.amount_inr ?? alert?.amount_inr ?? null;
  return { src, dst, amount };
}

/** Recent decisions table with All/Block/Challenge filters + row selection. */
export function DecisionTable({
  alerts,
  selectedTxId,
  onSelect,
}: {
  alerts: Alert[];
  selectedTxId: string | null;
  onSelect: (txId: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [rows, setRows] = useState<Decision[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const action = filter === "ALL" ? undefined : (filter as DecisionAction);
      const list = await fetchDecisions(LIMIT, action);
      if (seq !== requestSeq.current) return;
      setRows(list);
      setError("");
      setState("ready");
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(err));
      setState("error");
    }
  }, [filter]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const alertsByTx = useMemo(() => {
    const map = new Map<string, Alert>();
    for (const alert of alerts) map.set(alert.tx_id, alert);
    return map;
  }, [alerts]);

  const visible = useMemo(() => {
    const filtered = rows.filter(
      (d) => filter === "ALL" || d.action === filter,
    );
    return [...filtered].sort((a, b) => b.timestamp_ms - a.timestamp_ms);
  }, [rows, filter]);

  const filterButtons = FILTERS.map((f) => {
    const active = filter === f.id;
    return (
      <button
        key={f.id}
        type="button"
        onClick={() => setFilter(f.id)}
        aria-pressed={active}
        className={cx(
          "cursor-pointer rounded border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider transition-colors",
          active
            ? "border-zinc-500 bg-zinc-700/70 text-zinc-100"
            : "border-zinc-700/70 bg-zinc-900 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300",
        )}
      >
        {f.label}
      </button>
    );
  });

  return (
    <Panel
      title="Decision stream"
      subtitle={`last ${LIMIT} decisions · auto-refresh ${REFRESH_MS / 1_000}s`}
      right={filterButtons}
      bodyClassName="flex flex-col"
      className="max-h-[660px]"
    >
      {state === "error" && rows.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-10 text-center">
          <span className="rounded border border-red-600/40 bg-red-950/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-red-400">
            stream offline
          </span>
          <p className="max-w-[420px] break-all font-mono text-[10px] text-zinc-600" title={error}>
            {error}
          </p>
          <p className="text-[11px] text-zinc-500">
            Decision history needs stream-api on {STREAM_API_URL} — retrying
            every {REFRESH_MS / 1_000}s.
          </p>
        </div>
      ) : state === "loading" && rows.length === 0 ? (
        <div className="flex flex-1 items-center justify-center py-10 text-[11px] text-zinc-600">
          Loading decisions…
        </div>
      ) : visible.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 py-10 text-center">
          <p className="text-[11px] text-zinc-400">
            {rows.length === 0
              ? "No decisions yet."
              : `No ${filter.toLowerCase()} decisions in the last ${LIMIT}.`}
          </p>
          <p className="text-[10px] text-zinc-600">
            Start stream-api + the load generator to populate the stream.
          </p>
        </div>
      ) : null}

      {visible.length > 0 ? (
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full border-collapse text-left">
            <thead className="sticky top-0 z-10 bg-zinc-900 shadow-[0_1px_0_0_var(--color-zinc-800)]">
              <tr className="text-[9px] uppercase tracking-[0.14em] text-zinc-600">
                <th className="px-3 py-2 font-semibold">Time</th>
                <th className="px-3 py-2 font-semibold">Tx</th>
                <th className="px-3 py-2 font-semibold">Flow</th>
                <th className="px-3 py-2 text-right font-semibold">Amount</th>
                <th className="px-3 py-2 font-semibold">Score</th>
                <th className="px-3 py-2 font-semibold">Action</th>
                <th className="px-3 py-2 font-semibold">Top reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/60">
              {visible.map((decision) => {
                const meta = ACTION_META[decision.action];
                const flow = flowOf(decision, alertsByTx);
                const isSelected = decision.tx_id === selectedTxId;
                return (
                  <tr
                    key={decision.tx_id}
                    tabIndex={0}
                    onClick={() => onSelect(decision.tx_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelect(decision.tx_id);
                      }
                    }}
                    className={cx(
                      "cursor-pointer transition-colors hover:bg-zinc-800/50 focus-visible:bg-zinc-800/50 focus-visible:outline-none",
                      isSelected && "bg-zinc-800/70",
                    )}
                  >
                    <td
                      className="whitespace-nowrap px-3 py-1.5 font-mono text-[11px] tabular-nums text-slate-500"
                      title={formatDateTime(decision.timestamp_ms)}
                    >
                      {formatTime(decision.timestamp_ms)}
                    </td>
                    <td
                      className="whitespace-nowrap px-3 py-1.5 font-mono text-[11px] text-slate-300"
                      title={decision.tx_id}
                    >
                      {shortTx(decision.tx_id)}
                    </td>
                    <td className="max-w-[240px] truncate px-3 py-1.5 font-mono text-[11px] text-slate-400">
                      {flow.src && flow.dst ? (
                        <>
                          {shortVpa(flow.src, 18)}
                          <span className="mx-1 text-zinc-600">→</span>
                          {shortVpa(flow.dst, 18)}
                        </>
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-[11px] tabular-nums text-slate-300">
                      {flow.amount !== null ? `₹${formatInr(flow.amount)}` : "—"}
                    </td>
                    <td className="px-3 py-1.5">
                      <div className="flex items-center gap-2">
                        <span
                          className={cx(
                            "w-8 text-right font-mono text-[11px] font-semibold tabular-nums",
                            meta.text,
                          )}
                        >
                          {decision.score.toFixed(2)}
                        </span>
                        <span className="h-1.5 w-16 overflow-hidden rounded-full bg-zinc-800">
                          <span
                            className={cx("block h-full rounded-full", meta.bar)}
                            style={{ width: `${scorePercent(decision.score)}%` }}
                          />
                        </span>
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5">
                      <ActionBadge action={decision.action} />
                    </td>
                    <td
                      className="max-w-[220px] truncate px-3 py-1.5 font-mono text-[11px] text-slate-500"
                      title={decision.reasons.join(", ")}
                    >
                      {topReason(decision)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </Panel>
  );
}
