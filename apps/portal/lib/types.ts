/**
 * Wire types mirrored from `crates/upifraud-core/src/types.rs`.
 *
 * JSON on the wire is snake_case — field names below are identical to the
 * Rust serde output. Nothing is renamed for TypeScript ergonomics.
 */

/** Final disposition of a transaction (`DecisionAction`, serde UPPERCASE). */
export type DecisionAction = "ALLOW" | "CHALLENGE" | "BLOCK";

/** Per-stage latency accounting (ms) for the SLA readout. */
export interface LatencyBreakdown {
  ingest_ms: number;
  feature_ms: number;
  inference_ms: number;
  policy_ms: number;
  total_ms: number;
}

/** A single contributing factor to the score (`ScoreFactor`). */
export interface ScoreFactor {
  name: string;
  /** Signed contribution — positive → riskier. */
  weight: number;
}

/** Live alert pushed over `WS /ws/alerts` (decision subset + payment context). */
export interface Alert {
  tx_id: string;
  timestamp_ms: number;
  score: number;
  action: DecisionAction;
  reasons: string[];
  src_vpa: string;
  dst_vpa: string;
  amount_inr: number;
  model_version: string;
}

/**
 * Risk decision (`Decision` in the core crate).
 *
 * The core wire type is tx_id/timestamp_ms/score/action/reasons/top_factors/
 * model_version/latency. stream-api may additionally attach payment context
 * (`src_vpa`/`dst_vpa`/`amount_inr`, the same fields the alert carries) so the
 * table can show the flow — those are optional to stay tolerant either way.
 */
export interface Decision {
  tx_id: string;
  timestamp_ms: number;
  /** Blended risk score, 0..1. */
  score: number;
  action: DecisionAction;
  /** Reason codes, e.g. `HIGH_VELOCITY_1H`, `MULE_PATH_2HOP`. */
  reasons: string[];
  top_factors?: ScoreFactor[];
  model_version: string;
  latency: LatencyBreakdown;
  src_vpa?: string | null;
  dst_vpa?: string | null;
  amount_inr?: number | null;
}

/** A node of the 2-hop heterogeneous subgraph (`GraphNode`). */
export interface GraphNode {
  /** Stable id, e.g. `Account:alice@okaxis`, `Device:ab12…`. */
  id: string;
  /** `Account` | `Device` | `IPAddress` | `Merchant`. */
  label: string;
  props: Record<string, unknown>;
}

/** An edge of the 2-hop subgraph (`GraphEdge`). */
export interface GraphEdge {
  src: string;
  dst: string;
  /** `TRANSFERRED` | `USED_DEVICE` | `CONNECTED_FROM` | `PAID_MERCHANT`. */
  rel: string;
  amount?: number | null;
  timestamp_ms?: number | null;
}

/** `GET /v1/decisions/{tx_id}/graph` response. */
export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** `GET /v1/decisions/{tx_id}/explanation` response. */
export interface Explanation {
  tx_id: string;
  attributions: ScoreFactor[];
  /** Natural-language SOC brief from the local LLM (or template). */
  brief: string;
  /** e.g. `gnnexplainer+qwen2.5-7b` or `heuristic+template`. */
  engine: string;
}

/** `GET /v1/stats` response. */
export interface DecisionStats {
  ALLOW: number;
  CHALLENGE: number;
  BLOCK: number;
}

/** `GET /v1/decisions` response envelope. */
export interface DecisionsResponse {
  decisions: Decision[];
}

/** WebSocket frame envelope. First frame from the server is `{type:"hello"}`. */
export interface WsFrame {
  type: string;
  data?: Alert;
}
