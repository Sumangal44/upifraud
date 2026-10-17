import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AlertsProvider } from "@/components/AlertsProvider";
import { Header } from "@/components/Header";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "UPI Fraud · SOC Console",
    template: "%s · UPI Fraud SOC",
  },
  description:
    "Real-time SOC dashboard for the UPI fraud-detection platform: live alerts, risk decisions, 2-hop subgraphs and XAI briefs.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col font-sans text-slate-300 antialiased">
        <AlertsProvider>
          <Header />
          <main className="mx-auto w-full max-w-[1700px] flex-1 px-4 py-4">
            {children}
          </main>
          <footer className="border-t border-zinc-900 px-4 py-2 text-center font-mono text-[10px] text-zinc-700">
            risk policy · ≥0.80 BLOCK · 0.50–0.80 CHALLENGE (step-up) · &lt;0.50
            ALLOW · blend 0.6 GNN + 0.4 XGBoost
          </footer>
        </AlertsProvider>
      </body>
    </html>
  );
}
