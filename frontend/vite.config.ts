import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend address for the dev proxy; override e.g. BACKEND_URL=http://localhost:8001 npm run dev
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
    },
  },
});
