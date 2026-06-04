import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev harness for the review-ui components. The `@` alias matches tsconfig so
// the same `@/...` imports resolve in the editor, `tsc`, and the dev server.
//
// `/jobs/*` is proxied to the FastAPI backend (uvicorn api.app:app, :8000 by
// default) so the browser calls it same-origin — no CORS, and the preview's
// HTTP range requests pass straight through to FileResponse. Point at another
// backend with VITE_API_TARGET.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    open: false,
    proxy: {
      "/jobs": { target: API_TARGET, changeOrigin: true },
    },
  },
});
