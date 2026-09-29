import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "ATLAS Command Center",
    short_name: "ATLAS",
    start_url: "/",
    display: "standalone",
    background_color: "#04060b",
    theme_color: "#04060b",
    icons: [{ src: "/icon.svg", sizes: "any", type: "image/svg+xml" }],
  };
}
