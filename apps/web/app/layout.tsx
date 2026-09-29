import "./globals.css";
import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: "ATLAS Command Center",
  description: "One Intelligence. Many Agents.",
  // "Add to Home Screen" opens ATLAS full-screen, like an app
  appleWebApp: { capable: true, title: "ATLAS", statusBarStyle: "black-translucent" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#04060b",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="grid-bg min-h-screen antialiased">{children}</body>
    </html>
  );
}
