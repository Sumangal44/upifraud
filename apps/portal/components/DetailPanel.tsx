"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { errorMessage, fetchDecision, fetchGraph } from "@/lib/api";
import { ACTION_META, cx, formatDateTime, formatInr, shortVpa } from "@/lib/format";
import type { Alert, Decision, GraphResponse } from "@/lib/types";
import { ActionBadge } from "./ActionBadge";
import { ExplanationPanel } from "./ExplanationPanel";
import { GraphView } from "./GraphView";

type LoadState = "loading" | "ready" | "error";

const STAGES: { key: "ingest_ms" | "feature_ms" | "inference_ms" | "policy_ms"; label: string; bar: string }[] = [
  { key: "ingest_ms", label: "ingest", bar: "bg-slate-500" },
  { key: "feature_ms", label: "features", bar: "bg-sky-600" },
  { key: "inference_ms", label: "inference", bar: "bg-violet-500" },
  { key: "policy_ms", label: "policy", bar: "bg-amber-500" },
];

const SLA_MS = 100;

function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">
      {children}
    </h3>
  );
}

/**
 * Side drawer for one transaction: decision detail + latency breakdown,
 * 2-hop subgraph (Cytoscape) and on-demand XAI explanation.
 */
export function DetailPanel({
  txId,
  alert,
  onClose,
}: {
  txId: string;
  alert: Alert | null;
  onClose: () => void;
}) {
  const [decision, setDecision] = useState<Decision | null>(null);
  const [decisionState, setDecisionState] = useState<LoadState>("loading");
  const [decisionError, setDecisionError] = useState("");
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [graphState, setGraphState] = useState<LoadState>("loading");
  const [graphError, setGraphError] = useState("");

  useEffect(() => {
    let stale = false;
    setDecision(null);
    setDecisionState("loading");
    setDecisionError("");
    setGraph(null);
    setGraphState("loading");
    setGraphError("");

    void fetchDecision(txId)
      .then((d) => {
        if (stale) return;
        setDecision(d);
        setDecisionState("ready");
      })
      .catch((err: unknown) => {
        if (stale) return;
        setDecisionError(errorMessage(err));
        setDecisionState("error");
      });

    void fetchGraph(txId)
      .then((g) => {
        if (stale) return;
        setGraph({
          nodes: Array.isArray(g?.nodes) ? g.nodes : [],
          edges: Array.isArray(g?.edges) ? g.edges : [],
        });
        setGraphState("ready");
      })
      .catch((err: unknown) => {
        if (stale) return;
        setGraphError(errorMessage(err));
        setGraphState("error");
      });

    return () => {
      stale = true;
    };
  }, [txId]);

  // Esc closes the drawer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const srcVpa = decision?.src_vpa ?? alert?.src_vpa ?? null;
  const dstVpa = decision?.dst_vpa ?? alert?.dst_vpa ?? null;
  const amount = decision?.amount_inr ?? alert?.amount_inr ?? null;

  const partyVpas = useMemo(
    () => [srcVpa, dstVpa].filter((v): v is string => Boolean(v)),
    [srcVpa, dstVpa],
  );

  const meta = decision ? ACTION_META[decision.action] : null;
  const latency = decision?.latency;
  const maxStage = latency
    ? Math.max(1, ...STAGES.map((s) => latency[s.key] ?? 0))
    : 1;
  const totalMs = latency?.total_ms ?? null;

  return (
    <>
      <div
        className="fixed inset-0 z-30 bg-black/60"
        onClick={onClose}
        aria-hidden
      />
      <aside
        role="dialog"
        aria-label={`Transaction ${txId}`}
        className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-zinc-800 bg-zinc-950 shadow-[-24px_0_48px_rgba(0,0,0,0.5)]"
      >
        {/* Header ---------------------------------------------------------- */}
        <div className="flex items-start justify-between gap-3 border-b border-zinc-800 px-4 py-3">
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">
              Transaction detail
            </p>
            <p className="truncate font-mono text-xs text-slate-200" title={txId}>
              {txId}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close detail panel"
            className="cursor-pointer rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400 transition-colors hover:border-zinc-500 hover:text-zinc-200"
          >
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-3">
          {/* Decision summary --------------------------------------------- */}
          <section>
            <SectionHeading>Decision</SectionHeading>
            {decisionState === "loading" ? (
              <div className="h-16 animate-pulse rounded bg-zinc-900" />
            ) : decisionState === "error" ? (
              <div className="space-y-1">
                <p className="text-[11px] text-red-400">
                  Decision unavailable
                </p>
                <p className="break-all font-mono text-[10px] text-zinc-600" title={decisionError}>
                  {decisionError}
                </p>
                <p className="text-[10px] text-zinc-600">
                  Falls back to live-alert context below, if available.
                </p>
              </div>
            ) : decision ? (
              <div className="space-y-2.5">
                <div className="flex items-end gap-3">
                  <span
                    className={cx(
                      "text-5xl font-bold leading-none tabular-nums",
                      meta?.text ?? "text-zinc-300",
                    )}
                    title="blended risk score (0–1)"
                  >
                    {decision.score.toFixed(2)}
                  </span>
                  <div className="pb-1">
                    <ActionBadge action={decision.action} className="px-2 py-0.5" />
                    <p className="mt-1 text-[10px] text-zinc-600">
                      blended risk score
                    </p>
                  </div>
                  <div className="ml-auto pb-1 text-right">
                    <p className="font-mono text-[10px] text-zinc-500">
                      {decision.model_version}
                    </p>
                    <p className="text-[10px] text-zinc-600" title={formatDateTime(decision.timestamp_ms)}>
                      {formatDateTime(decision.timestamp_ms)}
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-1">
                  {decision.reasons.length > 0 ? (
                    decision.reasons.map((reason) => (
                      <span
                        key={reason}
                        className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-slate-300"
                      >
                        {reason}
                      </span>
                    ))
                  ) : (
                    <span className="font-mono text-[10px] text-zinc-600">
                      no reason codes
                    </span>
                  )}
                </div>

                {srcVpa && dstVpa ? (
                  <div className="flex flex-wrap items-center gap-2 rounded border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 font-mono text-[11px] text-slate-300">
                    <span title={srcVpa}>{shortVpa(srcVpa, 24)}</span>
                    <span className="text-zinc-600">→</span>
                    <span title={dstVpa}>{shortVpa(dstVpa, 24)}</span>
                    {amount !== null ? (
                      <span className="ml-auto tabular-nums text-slate-200">
                        ₹{formatInr(amount)}
                      </span>
                    ) : null}
                  </div>
                ) : null}

                {(decision.top_factors?.length ?? 0) > 0 ? (
                  <div>
                    <p className="mb-1 text-[10px] text-zinc-600">
                      Top model factors
                    </p>
                    <ul className="space-y-0.5">
                      {decision.top_factors?.slice(0, 5).map((factor) => (
                        <li
                          key={factor.name}
                          className="flex items-baseline justify-between gap-2 font-mono text-[10px]"
                        >
                          <span className="truncate text-slate-400" title={factor.name}>
                            {factor.name}
                          </span>
                          <span
                            className={cx(
                              "shrink-0 tabular-nums",
                              factor.weight >= 0 ? "text-red-400" : "text-emerald-400",
                            )}
                          >
                            {factor.weight >= 0 ? "+" : ""}
                            {factor.weight.toFixed(3)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : null}
          </section>

          {/* Latency -------------------------------------------------------- */}
          {decision && latency ? (
            <section>
              <SectionHeading>Latency breakdown</SectionHeading>
              <div className="space-y-1 rounded border border-zinc-800 bg-zinc-950/60 p-2">
                {STAGES.map((stage) => {
                  const ms = latency[stage.key] ?? 0;
                  return (
                    <div key={stage.key} className="flex items-center gap-2">
                      <span className="w-16 shrink-0 font-mono text-[10px] text-zinc-500">
                        {stage.label}
                      </span>
                      <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-900">
                        <span
                          className={cx("block h-full rounded-full", stage.bar)}
                          style={{ width: `${Math.round((ms / maxStage) * 100)}%` }}
                        />
                      </span>
                      <span className="w-14 shrink-0 text-right font-mono text-[10px] tabular-nums text-slate-400">
                        {ms} ms
                      </span>
                    </div>
                  );
                })}
                <div className="flex items-center gap-2 border-t border-zinc-800 pt-1">
                  <span className="w-16 shrink-0 font-mono text-[10px] font-semibold text-zinc-400">
                    total
                  </span>
                  <span className="flex-1">
                    <span
                      className={cx(
                        "rounded border px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wider",
                        (totalMs ?? 0) <= SLA_MS
                          ? "border-emerald-700/50 bg-emerald-950/50 text-emerald-400"
                          : "border-red-700/50 bg-red-950/50 text-red-400",
                      )}
                      title={`sub-${SLA_MS} ms decisioning SLA`}
                    >
                      {(totalMs ?? 0) <= SLA_MS ? "SLA met" : "SLA breach"}
                    </span>
                  </span>
                  <span className="w-14 shrink-0 text-right font-mono text-[11px] font-semibold tabular-nums text-slate-200">
                    {totalMs ?? "—"} ms
                  </span>
                </div>
              </div>
            </section>
          ) : null}

          {/* Graph ---------------------------------------------------------- */}
          <section>
            <SectionHeading>2-hop subgraph</SectionHeading>
            <GraphView
              graph={graph}
              loading={graphState === "loading"}
              error={graphState === "error" ? graphError : null}
              partyVpas={partyVpas}
            />
          </section>

          {/* Explanation ---------------------------------------------------- */}
          <section>
            <SectionHeading>Explainability</SectionHeading>
            <ExplanationPanel txId={txId} />
          </section>
        </div>
      </aside>
    </>
  );
}
