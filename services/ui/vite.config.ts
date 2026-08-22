import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // `pnpm dev` outside Docker talks to the API container directly.
    proxy: { "/api": "http://localhost:8300" },
  },
});
