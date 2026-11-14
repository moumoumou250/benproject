import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The viewer is served as static files by a stdlib Python server on an
// arbitrary ephemeral port, so all asset URLs must be relative (base: "./").
// The build output is committed at strix/interface/viewer/static and shipped.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  // Dev server only. The Python console serves /api and the built files on
  // the same origin; Vite does not, so an unproxied /api/* falls through to
  // index.html and response.json() throws
  // "Unexpected token '<' ... is not valid JSON".
  server: {
    proxy: {
      "/api": {
        target: process.env.BEN_VIEWER_ORIGIN ?? "http://127.0.0.1:8765",
      },
    },
  },
});
