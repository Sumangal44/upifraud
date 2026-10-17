import type { ReactNode } from "react";
import { cx } from "@/lib/format";

/** Dense bordered panel used by every dashboard module. */
export function Panel({
  title,
  subtitle,
  right,
  bodyClassName,
  className,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
  bodyClassName?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      className={cx(
        "flex min-w-0 flex-col rounded-md border border-zinc-800 bg-zinc-900/60",
        className,
      )}
    >
      <header className="flex items-center justify-between gap-3 border-b border-zinc-800 px-3 py-2">
        <div className="min-w-0">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-zinc-400">
            {title}
          </h2>
          {subtitle ? (
            <p className="truncate text-[10px] text-zinc-600">{subtitle}</p>
          ) : null}
        </div>
        {right ? <div className="flex shrink-0 items-center gap-2">{right}</div> : null}
      </header>
      <div className={cx("min-h-0 flex-1", bodyClassName)}>{children}</div>
    </section>
  );
}
