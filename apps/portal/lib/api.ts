/**
 * Typed fetch helpers for the stream-api REST surface.
 *
 * All failures reject with an `ApiError` carrying a human-readable message —
 * callers surface the message directly in the UI's degraded/offline states.
 */
import type {
  Decision,
  DecisionAction,
  DecisionStats,
  DecisionsResponse,
  Explanation,
  GraphResponse,
} from "./types";

const DEFAULT_STREAM_API_URL = "http://localhost:8081";
const REQUEST_TIMEOUT_MS = 8_000;

function resolveBaseUrl(): string {
  const raw =
    process.env.NEXT_PUBLIC_STREAM_API_URL?.trim() || DEFAULT_STREAM_API_URL;
  return raw.replace(/\/+$/, "");
}

/** Base HTTP URL of stream-api, e.g. `http://localhost:8081`. */
export const STREAM_API_URL = resolveBaseUrl();

/** WebSocket endpoint of stream-api, e.g. `ws://localhost:8081/ws/alerts`. */
export const WS_ALERTS_URL = `${STREAM_API_URL.replace(/^http/, "ws")}/ws/alerts`;

export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Flatten unknown thrown values into a displayable message. */
export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

async function getJson<T>(path: string): Promise<T> {
  const url = `${STREAM_API_URL}${path}`;

  let res: Response;
  try {
    res = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : "network error";
    throw new ApiError(`stream-api unreachable — ${detail} (${url})`);
  }

  if (!res.ok) {
    throw new ApiError(`GET ${path} failed — HTTP ${res.status}`, res.status);
  }

  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError(`GET ${path} returned invalid JSON`, res.status);
  }
}

/** `GET /v1/stats` — decision counts per action. */
export async function fetchStats(): Promise<DecisionStats> {
  const raw = await getJson<Partial<DecisionStats>>("/v1/stats");
  const num = (v: unknown): number =>
    typeof v === "number" && Number.isFinite(v) ? v : 0;
  return {
    ALLOW: num(raw?.ALLOW),
    CHALLENGE: num(raw?.CHALLENGE),
    BLOCK: num(raw?.BLOCK),
  };
}

/** `GET /v1/decisions?limit=&action=` — recent decisions, newest first. */
export async function fetchDecisions(
  limit = 50,
  action?: DecisionAction,
): Promise<Decision[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (action) params.set("action", action);
  const body = await getJson<DecisionsResponse>(
    `/v1/decisions?${params.toString()}`,
  );
  return Array.isArray(body?.decisions) ? body.decisions : [];
}

/** `GET /v1/decisions/{tx_id}` — full decision incl. latency breakdown. */
export function fetchDecision(txId: string): Promise<Decision> {
  return getJson<Decision>(`/v1/decisions/${encodeURIComponent(txId)}`);
}

/** `GET /v1/decisions/{tx_id}/graph` — 2-hop subgraph (may be empty). */
export function fetchGraph(txId: string): Promise<GraphResponse> {
  return getJson<GraphResponse>(
    `/v1/decisions/${encodeURIComponent(txId)}/graph`,
  );
}

/** `GET /v1/decisions/{tx_id}/explanation` — XAI attributions + brief. */
export function fetchExplanation(txId: string): Promise<Explanation> {
  return getJson<Explanation>(
    `/v1/decisions/${encodeURIComponent(txId)}/explanation`,
  );
}
