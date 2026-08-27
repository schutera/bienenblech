import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In production FastAPI serves the built SPA from frontend/dist at /, so the app
// only ever talks to same-origin /api. The dev proxy reproduces that: without it
// the session cookie would be cross-origin and every call would need
// credentials:"include" plus CORS, which production does not have.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
