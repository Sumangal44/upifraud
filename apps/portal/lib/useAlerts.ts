"use client";

/**
 * Live alert stream hook.
 *
 * Connects to `WS /ws/alerts` on mount and reconnects forever with capped
 * exponential backoff (+ jitter). Keeps the newest {@link MAX_ALERTS} alerts,
 * newest first. Status transitions: `connecting` → `live` → `offline` →
 * `connecting` … so the header can render a truthful connection dot even when
 * stream-api is not running.
 */
import { useEffect, useState } from "react";
import { WS_ALERTS_URL } from "./api";
import type { Alert, WsFrame } from "./types";

export const MAX_ALERTS = 100;
const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const JITTER_MS = 500;

export type StreamStatus = "connecting" | "live" | "offline";

export interface AlertsStream {
  status: StreamStatus;
  alerts: Alert[];
}

export function useAlertsSocket(): AlertsStream {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let attempt = 0;
    let disposed = false;

    const scheduleReconnect = (): void => {
      if (disposed) return;
      setStatus("offline");
      const delay =
        Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS) +
        Math.random() * JITTER_MS;
      attempt += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };

    const connect = (): void => {
      if (disposed) return;
      setStatus("connecting");

      let ws: WebSocket;
      try {
        ws = new WebSocket(WS_ALERTS_URL);
      } catch {
        // `new WebSocket` itself threw (invalid URL / blocked env).
        scheduleReconnect();
        return;
      }
      socket = ws;

      ws.onopen = () => {
        if (disposed) return;
        attempt = 0;
        setStatus("live");
      };

      ws.onmessage = (event: MessageEvent<string>) => {
        if (disposed) return;
        let frame: WsFrame;
        try {
          frame = JSON.parse(event.data) as WsFrame;
        } catch {
          return; // ignore malformed frames
        }
        if (frame?.type === "alert" && frame.data?.tx_id) {
          const alert = frame.data;
          setAlerts((prev) => [alert, ...prev].slice(0, MAX_ALERTS));
        }
        // `{"type":"hello"}` and unknown frame types need no handling.
      };

      // `onclose` always follows an error — backoff happens there only.
      ws.onerror = () => undefined;

      ws.onclose = () => {
        if (socket === ws) socket = null;
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };
  }, []);

  return { status, alerts };
}
