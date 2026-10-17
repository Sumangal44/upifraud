"use client";

import { useCallback, useState } from "react";
import { AlertFeed } from "@/components/AlertFeed";
import { useAlerts } from "@/components/AlertsProvider";
import { DecisionTable } from "@/components/DecisionTable";
import { DetailPanel } from "@/components/DetailPanel";
import { StatCards } from "@/components/StatCards";
import type { Alert } from "@/lib/types";

interface Selection {
  txId: string;
  alert: Alert | null;
}

function Dashboard() {
  const { alerts } = useAlerts();
  const [selection, setSelection] = useState<Selection | null>(null);

  const selectTx = useCallback(
    (txId: string, alert: Alert | null) => setSelection({ txId, alert }),
    [],
  );

  const handleTableSelect = useCallback(
    (txId: string) => {
      // Reuse live-alert context (VPAs/amount) when we have it for this tx.
      const alert = alerts.find((a) => a.tx_id === txId) ?? null;
      selectTx(txId, alert);
    },
    [alerts, selectTx],
  );

  const handleAlertSelect = useCallback(
    (alert: Alert) => selectTx(alert.tx_id, alert),
    [selectTx],
  );

  return (
    <div className="space-y-4">
      <StatCards />

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_370px]">
        <DecisionTable
          alerts={alerts}
          selectedTxId={selection?.txId ?? null}
          onSelect={handleTableSelect}
        />
        <AlertFeed
          onSelect={handleAlertSelect}
          selectedTxId={selection?.txId ?? null}
        />
      </div>

      {selection ? (
        <DetailPanel
          txId={selection.txId}
          alert={selection.alert}
          onClose={() => setSelection(null)}
        />
      ) : null}
    </div>
  );
}

/** SOC dashboard entry point (client-heavy; shell is prerendered).
 *  The alert socket lives in the layout's <AlertsProvider>. */
export default function DashboardPage() {
  return <Dashboard />;
}
