"use client";

import { useEffect, useState } from "react";
import { errorMessage, fetchExplanation } from "@/lib/api";
import { cx } from "@/lib/format";
import type { Explanation } from "@/lib/types";

type State = "idle" | "loading" | "done" | "error";

/**
 * XAI readout for a decision. The explanation is fetched only when the analyst
 * presses "Generate explanation" (then re-runnable) so the XAI service is not
 * hammered by page traffic.
 */
export function ExplanationPanel({ txId }: { txId: string }) {
  const [state, setState] = useState<State>("idle");
  const [data, setData] = useState<Explanation | null>(null);
  const [error, setError] = useState("");

  // Reset whenever the drawer moves to another transaction.
  useEffect(() => {
    setState("idle");
    setData(null);
    setError("");
  }, [txId]);

  const run = async (): Promise<void> => {
    setState("loading");
    setError("");
    try {
      const result = await fetchExplanation(txId);
      setData(result);
      setState("done");
    } catch (err) {
      setError(errorMessage(err));
      setState("error");
    }
  };

  const attributions = data?.attributions ?? [];
  const maxAbs = Math.max(
    1e-9,
    ...attributions.map((a) => Math.abs(a.weight)),
  );

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950/50 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">
          Explanation (XAI)
        </h3>
        <button
          type="button"
          onClick={() => void run()}
          disabled={state === "loading"}
          className={cx(
            "cursor-pointer rounded border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider transition-colors disabled:cursor-wait",
            state === "done"
              ? "border-zinc-600 bg-zinc-800 text-zinc-300 hover:bg-zinc-700"
              : "border-cyan-700/60 bg-cyan-950/50 text-cyan-300 hover:bg-cyan-900/40",
            state === "loading" && "opacity-60",
          )}
        >
          {state === "loading"
            ? "Explaining…"
            : state === "done"
              ? "Re-run"
              : "Generate explanation"}
        </button>
      </div>

      <div className="mt-2">
        {state === "idle" ? (
          <p className="text-[11px] text-zinc-600">
            Not generated yet — attributions come from GNNExplainer/SHAP and
            are computed on demand (air-gapped XAI agent).
          </p>
        ) : null}

        {state === "error" ? (
          <div className="space-y-1">
            <p className="text-[11px] text-red-400">Explanation failed</p>
            <p className="break-all font-mono text-[10px] text-zinc-600" title={error}>
              {error}
            </p>
          </div>
        ) : null}

        {state === "done" && data ? (
          <div className="space-y-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded border border-cyan-700/50 bg-cyan-950/40 px-1.5 py-0.5 font-mono text-[10px] text-cyan-400">
                {data.engine}
              </span>
              <span className="font-mono text-[10px] text-zinc-600">
                {attributions.length} attribution(s)
              </span>
            </div>

            {attributions.length === 0 ? (
              <p className="text-[11px] text-zinc-600">
                No attributions returned for this decision.
              </p>
            ) : (
              <ul className="space-y-1">
                {attributions.map((attr) => {
                  const positive = attr.weight >= 0;
                  const widthPct = (Math.abs(attr.weight) / maxAbs) * 50;
                  return (
                    <li
                      key={attr.name}
                      className="grid grid-cols-[minmax(0,1fr)_96px_64px] items-center gap-2"
                    >
                      <span
                        className="truncate font-mono text-[10px] text-slate-400"
                        title={attr.name}
                      >
                        {attr.name}
                      </span>
                      <span className="relative block h-2 rounded-sm bg-zinc-800">
                        <span className="absolute inset-y-0 left-1/2 w-px bg-zinc-600" />
                        <span
                          className={cx(
                            "absolute top-0 block h-2 rounded-sm",
                            positive ? "left-1/2 bg-red-500" : "right-1/2 bg-emerald-500",
                          )}
                          style={{ width: `${widthPct}%` }}
                        />
                      </span>
                      <span
                        className={cx(
                          "text-right font-mono text-[10px] tabular-nums",
                          positive ? "text-red-400" : "text-emerald-400",
                        )}
                      >
                        {positive ? "+" : ""}
                        {attr.weight.toFixed(3)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}

            {data.brief ? (
              <p className="whitespace-pre-wrap rounded border-l-2 border-zinc-700 bg-zinc-900/60 px-2 py-1.5 text-[11px] leading-relaxed text-slate-300">
                {data.brief}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
