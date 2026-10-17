import { ACTION_META, cx } from "@/lib/format";
import type { DecisionAction } from "@/lib/types";

/** Compact coloured action pill (`ALLOW` / `CHALLENGE` / `BLOCK`). */
export function ActionBadge({
  action,
  className,
}: {
  action: DecisionAction;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center rounded border px-1.5 py-px font-mono text-[10px] font-semibold tracking-wider",
        ACTION_META[action].badge,
        className,
      )}
    >
      {action}
    </span>
  );
}
