"use client";

import { createContext, useContext, type ReactNode } from "react";
import { useAlertsSocket, type AlertsStream } from "@/lib/useAlerts";

/**
 * Single WebSocket for the whole app: the provider owns the connection that
 * both the header status dot and the alert feed read from.
 */
const AlertsContext = createContext<AlertsStream | null>(null);

export function AlertsProvider({ children }: { children: ReactNode }) {
  const stream = useAlertsSocket();
  return (
    <AlertsContext.Provider value={stream}>{children}</AlertsContext.Provider>
  );
}

export function useAlerts(): AlertsStream {
  const stream = useContext(AlertsContext);
  if (!stream) {
    throw new Error("useAlerts must be used within <AlertsProvider>");
  }
  return stream;
}
