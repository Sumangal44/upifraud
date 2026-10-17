"use client";

/**
 * 2-hop subgraph viewer (Cytoscape.js).
 *
 * Node shape/colour encodes the entity label, edges are labelled with their
 * relationship, and the transaction's source/destination accounts get an amber
 * ring ("party" highlight). Tapping a node/edge dumps its properties into the
 * inspector below the canvas. fCoSE layout is used when the optional
 * `cytoscape-fcose` extension loads; otherwise built-in layouts are offered.
 *
 * Cytoscape is imported dynamically (client-only) and typed structurally here
 * to keep the component decoupled from cytoscape's bundled `.d.ts`.
 */
import { useEffect, useRef, useState } from "react";
import { cx } from "@/lib/format";
import type { GraphEdge, GraphNode, GraphResponse } from "@/lib/types";

type LayoutId = "fcose" | "breadthfirst" | "cose" | "grid";

const LAYOUTS: { id: LayoutId; label: string; title: string }[] = [
  { id: "fcose", label: "fCoSE", title: "force-directed (fcose)" },
  { id: "breadthfirst", label: "tree", title: "breadth-first tree" },
  { id: "cose", label: "CoSE", title: "compound spring embedder" },
  { id: "grid", label: "grid", title: "grid" },
];

const KIND_STYLE: Record<
  string,
  { shape: string; fill: string; border: string }
> = {
  Account: { shape: "ellipse", fill: "#10b981", border: "#047857" },
  Device: { shape: "round-rectangle", fill: "#8b5cf6", border: "#6d28d9" },
  IPAddress: { shape: "hexagon", fill: "#38bdf8", border: "#0284c7" },
  Merchant: { shape: "diamond", fill: "#ec4899", border: "#be185d" },
};

const FONT = "ui-monospace, SFMono-Regular, Menlo, monospace";

// ---------------------------------------------------------------------------
// Minimal structural types for what we use of cytoscape
// ---------------------------------------------------------------------------

interface CyLike {
  destroy(): void;
  resize(): void;
  elements(): { removeClass(className: string): void };
  on(event: string, selectorOrHandler: unknown, handler?: CyHandler): void;
}

type CyHandler = (event: { target: CyTargetLike }) => void;

interface CyTargetLike {
  data(): Record<string, unknown>;
  addClass(className: string): void;
}

interface CytoscapeFactory {
  (options: Record<string, unknown>): CyLike;
  use(extension: unknown): void;
}

async function loadCytoscape(): Promise<{
  cytoscape: CytoscapeFactory;
  fcose: boolean;
}> {
  const mod = (await import("cytoscape")) as unknown as {
    default?: CytoscapeFactory;
  } & CytoscapeFactory;
  const cytoscape: CytoscapeFactory = mod.default ?? mod;

  let fcose = false;
  try {
    const fcoseMod = (await import("cytoscape-fcose")) as unknown as {
      default?: unknown;
    } & Record<string, unknown>;
    const extension = fcoseMod.default ?? fcoseMod;
    if (typeof extension === "function") {
      cytoscape.use(extension);
      fcose = true;
    }
  } catch {
    fcose = false; // optional layout — built-in layouts remain available
  }

  return { cytoscape, fcose };
}

// ---------------------------------------------------------------------------
// Elements & stylesheet
// ---------------------------------------------------------------------------

function displayOf(node: GraphNode): string {
  const colon = node.id.indexOf(":");
  const raw = colon >= 0 ? node.id.slice(colon + 1) : node.id;
  return raw.length > 18 ? `${raw.slice(0, 17)}…` : raw;
}

function matchesParty(node: GraphNode, vpas: string[]): boolean {
  if (node.label !== "Account") return false;
  const display = displayOf(node);
  return vpas.some(
    (vpa) => vpa.length > 0 && (node.id === `Account:${vpa}` || display === vpa),
  );
}

function buildElements(graph: GraphResponse, vpas: string[]): unknown[] {
  const ids = new Set(graph.nodes.map((n) => n.id));
  const nodes = graph.nodes.map((n) => ({
    data: {
      id: n.id,
      kind: n.label,
      display: displayOf(n),
      props: n.props ?? {},
      is_party: matchesParty(n, vpas),
    },
    classes: matchesParty(n, vpas) ? "party" : "",
  }));
  const edges = graph.edges
    .filter((e) => ids.has(e.src) && ids.has(e.dst))
    .map((e: GraphEdge, index: number) => ({
      data: {
        id: `e${index}::${e.src}->${e.dst}`,
        source: e.src,
        target: e.dst,
        rel: e.rel,
        amount: e.amount ?? null,
        timestamp_ms: e.timestamp_ms ?? null,
      },
    }));
  return [...nodes, ...edges];
}

const STYLESHEET: unknown[] = [
  {
    selector: "node",
    style: {
      width: 30,
      height: 30,
      "background-color": "#3f3f46",
      "border-width": 2,
      "border-color": "#52525b",
      label: "data(display)",
      "font-size": 9,
      "font-family": FONT,
      color: "#a1a1aa",
      "text-valign": "bottom",
      "text-margin-y": 4,
      "text-wrap": "wrap",
      "text-max-width": "100px",
      "overlay-opacity": 0,
    },
  },
  ...Object.entries(KIND_STYLE).map(([kind, style]) => ({
    selector: `node[kind = "${kind}"]`,
    style: {
      shape: style.shape,
      "background-color": style.fill,
      "border-color": style.border,
    },
  })),
  {
    selector: "node.party",
    style: {
      width: 46,
      height: 46,
      "border-color": "#f59e0b",
      "border-width": 3,
      "shadow-blur": 16,
      "shadow-color": "#f59e0b",
      "shadow-opacity": 0.65,
      "shadow-offset": 0,
      color: "#fcd34d",
      "font-weight": 600,
    },
  },
  {
    selector: "node.inspected",
    style: {
      "border-color": "#fafafa",
      "border-width": 3,
    },
  },
  {
    selector: "edge",
    style: {
      width: 1.5,
      "line-color": "#3f3f46",
      "target-arrow-color": "#3f3f46",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      label: "data(rel)",
      "font-size": 8,
      "font-family": FONT,
      color: "#71717a",
      "text-rotation": "autorotate",
      "text-background-color": "#09090b",
      "text-background-opacity": 0.85,
      "text-background-padding": 2,
      "overlay-opacity": 0,
    },
  },
  {
    selector: 'edge[rel = "TRANSFERRED"]',
    style: {
      width: 2.2,
      "line-color": "#52525b",
      "target-arrow-color": "#52525b",
    },
  },
];

// ---------------------------------------------------------------------------
// Inspector formatting
// ---------------------------------------------------------------------------

interface InspectorState {
  title: string;
  kind: string;
  rows: Array<[string, string]>;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      const json = JSON.stringify(value);
      return json.length > 140 ? `${json.slice(0, 139)}…` : json;
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function propRows(props: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(props ?? {}).map(([k, v]) => [k, formatValue(v)]);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function GraphView({
  graph,
  loading,
  error,
  partyVpas,
}: {
  graph: GraphResponse | null;
  loading: boolean;
  error: string | null;
  partyVpas: string[];
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [layout, setLayout] = useState<LayoutId>("fcose");
  const [layoutAvailable, setLayoutAvailable] = useState(true);
  const [inspector, setInspector] = useState<InspectorState | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);

  const layoutKey = layout;
  const vpasKey = partyVpas.join("|");

  useEffect(() => {
    setInspector(null);
    setRenderError(null);
    if (!graph || graph.nodes.length === 0) return;

    const container = containerRef.current;
    if (!container) return;

    let disposed = false;
    let cy: CyLike | null = null;
    let observer: ResizeObserver | null = null;

    const run = async (): Promise<void> => {
      const { cytoscape, fcose } = await loadCytoscape();
      if (disposed) return;

      const wantsFcose = layoutKey === "fcose";
      if (wantsFcose && !fcose) {
        // Optional layout package missing — switch to a built-in layout.
        setLayoutAvailable(false);
        setLayout("breadthfirst");
      }
      const effective: LayoutId =
        wantsFcose && !fcose ? "breadthfirst" : layoutKey;

      const vpas = vpasKey ? vpasKey.split("|") : [];
      const elements = buildElements(graph, vpas);

      const create = (name: LayoutId): CyLike =>
        cytoscape({
          container,
          elements,
          style: STYLESHEET,
          layout: { name, animate: false, padding: 24 },
          wheelSensitivity: 0.2,
          minZoom: 0.25,
          maxZoom: 4,
        }) as CyLike;

      let created: CyLike | null = null;
      try {
        created = create(effective);
      } catch {
        if (effective === "breadthfirst") throw new Error("layout failed");
        created = create("breadthfirst");
        setLayout("breadthfirst");
        setLayoutAvailable(false);
      }
      if (!created || disposed) {
        created?.destroy();
        return;
      }
      cy = created;
      const instance = created;

      instance.on("tap", "node", (event) => {
        const data = event.target.data();
        instance.elements().removeClass("inspected");
        event.target.addClass("inspected");
        setInspector({
          title: String(data.id ?? ""),
          kind: String(data.kind ?? "node"),
          rows: propRows((data.props as Record<string, unknown>) ?? {}),
        });
      });

      instance.on("tap", "edge", (event) => {
        const data = event.target.data();
        setInspector({
          title: String(data.rel ?? "edge"),
          kind: "relationship",
          rows: [
            ["source", String(data.source ?? "")],
            ["target", String(data.target ?? "")],
            ["rel", String(data.rel ?? "")],
            ["amount", formatValue(data.amount)],
            ["timestamp_ms", formatValue(data.timestamp_ms)],
          ],
        });
      });

      instance.on("tap", (event: { target: CyTargetLike }) => {
        // Tap on the background clears the inspector.
        if (event.target === (instance as unknown as object)) {
          instance.elements().removeClass("inspected");
          setInspector(null);
        }
      });

      observer = new ResizeObserver(() => instance.resize());
      observer.observe(container);
    };

    void run().catch((err: unknown) => {
      if (disposed) return;
      setRenderError(err instanceof Error ? err.message : String(err));
    });

    return () => {
      disposed = true;
      observer?.disconnect();
      cy?.destroy();
    };
  }, [graph, layoutKey, vpasKey]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const nodeCount = graph?.nodes.length ?? 0;
  const edgeCount = graph?.edges.length ?? 0;

  if (loading) {
    return (
      <div className="flex h-56 items-center justify-center rounded border border-zinc-800 bg-zinc-950/60 text-[11px] text-zinc-600">
        Loading subgraph…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-56 flex-col items-center justify-center gap-1 rounded border border-red-900/60 bg-red-950/20 px-4 text-center">
        <span className="text-[11px] text-red-400">Subgraph unavailable</span>
        <span className="break-all font-mono text-[10px] text-zinc-600" title={error}>
          {error}
        </span>
      </div>
    );
  }

  if (!graph || nodeCount === 0) {
    return (
      <div className="flex h-56 flex-col items-center justify-center gap-1 rounded border border-dashed border-zinc-800 bg-zinc-950/40 px-4 text-center">
        <span className="text-[11px] text-zinc-500">
          No subgraph recorded for this transaction
        </span>
        <span className="text-[10px] text-zinc-600">
          The 2-hop neighbourhood came back empty (seed data may be missing).
        </span>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] tabular-nums text-zinc-500">
          {nodeCount} nodes · {edgeCount} edges
        </span>
        <div className="flex items-center gap-1">
          {LAYOUTS.filter(
            (l) => l.id !== "fcose" || layoutAvailable,
          ).map((l) => (
            <button
              key={l.id}
              type="button"
              title={l.title}
              onClick={() => setLayout(l.id)}
              className={cx(
                "cursor-pointer rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors",
                layout === l.id
                  ? "border-zinc-500 bg-zinc-700/70 text-zinc-100"
                  : "border-zinc-800 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300",
              )}
            >
              {l.label}
            </button>
          ))}
        </div>
      </div>

      {renderError ? (
        <div
          className="mb-1.5 rounded border border-amber-800/60 bg-amber-950/30 px-2 py-1 font-mono text-[10px] text-amber-400"
          title={renderError}
        >
          graph renderer error — {renderError}
        </div>
      ) : null}

      <div
        ref={containerRef}
        className="h-72 w-full rounded border border-zinc-800 bg-[#0a0a0c]"
        data-testid="graph-canvas"
      />

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500">
        {Object.entries(KIND_STYLE).map(([kind, style]) => (
          <span key={kind} className="flex items-center gap-1">
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ backgroundColor: style.fill }}
            />
            {kind}
          </span>
        ))}
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm border-2 border-amber-400 bg-zinc-800" />
          tx party
        </span>
      </div>

      {inspector ? (
        <div className="mt-2 rounded border border-zinc-800 bg-zinc-950/70 p-2">
          <div className="flex items-baseline justify-between gap-2">
            <span className="truncate font-mono text-[11px] text-slate-300" title={inspector.title}>
              {inspector.title}
            </span>
            <span className="shrink-0 rounded bg-zinc-800 px-1 font-mono text-[9px] uppercase tracking-wider text-zinc-500">
              {inspector.kind}
            </span>
          </div>
          {inspector.rows.length > 0 ? (
            <dl className="mt-1 grid grid-cols-[minmax(0,140px)_minmax(0,1fr)] gap-x-2 gap-y-0.5">
              {inspector.rows.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="truncate font-mono text-[10px] text-zinc-600" title={key}>
                    {key}
                  </dt>
                  <dd className="truncate font-mono text-[10px] text-slate-400" title={value}>
                    {value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="mt-1 font-mono text-[10px] text-zinc-600">
              no properties
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
