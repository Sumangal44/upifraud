"use client";

import { WS_ALERTS_URL } from "@/lib/api";
import { ACTION_META, cx, formatInr, formatTime, scorePercent, shortVpa } from "@/lib/format";
import type { Alert } from "@/lib/types";
import { useAlerts } from "./AlertsProvider";
import { ActionBadge } from "./ActionBadge";
import { Panel } from "./Panel";

/** Live WebSocket alert feed — newest first, capped at MAX_ALERTS items. */
export function AlertFeed({
  onSelect,
  selectedTxId,
}: {
  onSelect: (alert: Alert) => void;
  selectedTxId: string | null;
}) {
  const { alerts, status } = useAlerts();

  const statusChip =
    status === "live"
      ? "border-emerald-600/40 bg-emerald-950/60 text-emerald-400"
      : status === "connecting"
        ? "border-amber-600/40 bg-amber-950/60 text-amber-400"
        : "border-red-600/40 bg-red-950/60 text-red-400";

  const statusLabel =
    status === "live" ? "live" : status === "connecting" ? "dialing" : "offline";

  return (
    <Panel
      title="Live alerts"
      subtitle="WS /ws/alerts · newest first"
      right={
        <>
          <span
            className={cx(
              "rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider",
              statusChip,
            )}
            data-status={status}
          >
            {statusLabel}
          </span>
          <span className="font-mono text-[10px] tabular-nums text-zinc-500">
            {alerts.length}/100
          </span>
        </>
      }
      bodyClassName="flex flex-col"
      className="max-h-[660px]"
    >
      {alerts.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-10 text-center">
          <span
            className={cx(
              "h-2 w-2 rounded-full",
              status === "live"
                ? "bg-emerald-400 animate-pulse"
                : status === "connecting"
                  ? "bg-amber-400 animate-pulse"
                  : "bg-red-500",
            )}
          />
          <p className="text-[11px] text-zinc-400">
            {status === "live"
              ? "Connected — awaiting alerts…"
              : status === "connecting"
                ? "Connecting to alert stream…"
                : "Stream offline — no live alerts"}
          </p>
          <p className="max-w-[260px] break-all font-mono text-[10px] text-zinc-600">
            {WS_ALERTS_URL}
          </p>
        </div>
      ) : (
        <ul className="min-h-0 flex-1 divide-y divide-zinc-800/70 overflow-y-auto">
          {alerts.map((alert, index) => {
            const meta = ACTION_META[alert.action];
            const isSelected = alert.tx_id === selectedTxId;
            return (
              <li key={`${alert.tx_id}-${index}`}>
                <button
                  type="button"
                  onClick={() => onSelect(alert)}
                  className={cx(
                    "w-full cursor-pointer px-3 py-2 text-left transition-colors hover:bg-zinc-800/40",
                    isSelected && "bg-zinc-800/70",
                  )}
                  style={{
                    borderLeft: `3px solid ${meta.hex}`,
                  }}
                >
                  <div className="flex items-center gap-2">
                    <ActionBadge action={alert.action} />
                    <span
                      className={cx(
                        "rounded border bg-zinc-900 px-1 py-px font-mono text-[10px] font-semibold tabular-nums",
                        meta.badge,
                      )}
                      title={`risk score ${alert.score.toFixed(3)} (${scorePercent(alert.score)}%)`}
                    >
                      {alert.score.toFixed(2)}
                    </span>
                    <span
                      className="ml-auto font-mono text-[10px] tabular-nums text-zinc-500"
                      title={new Date(alert.timestamp_ms).toISOString()}
                    >
                      {formatTime(alert.timestamp_ms)}
                    </span>
                  </div>

                  <div className="mt-1 truncate font-mono text-[11px] text-slate-300">
                    {shortVpa(alert.src_vpa, 22)}
                    <span className="mx-1 text-zinc-600">→</span>
                    {shortVpa(alert.dst_vpa, 22)}
                  </div>

                  <div className="mt-1 flex items-center gap-2 text-[10px]">
                    <span className="font-mono tabular-nums text-slate-400">
                      ₹{formatInr(alert.amount_inr)}
                    </span>
                    {alert.reasons[0] ? (
                      <span
                        className="truncate font-mono text-zinc-500"
                        title={alert.reasons.join(", ")}
                      >
                        {alert.reasons[0]}
                      </span>
                    ) : null}
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
