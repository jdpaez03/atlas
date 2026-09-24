import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = { title: "ATLAS Command Center", description: "One Intelligence. Many Agents." };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="grid-bg min-h-screen antialiased">{children}</body>
    </html>
  );
}
