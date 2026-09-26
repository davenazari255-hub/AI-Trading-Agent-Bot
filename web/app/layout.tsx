import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Mooo",
  description: "Autonomous AI trading agent for Bybit USDT perpetuals",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          background: "#0b0e11",
          color: "#e6e8ea",
          fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
        }}
      >
        {children}
      </body>
    </html>
  );
}
