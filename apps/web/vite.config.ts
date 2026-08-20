import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Beside the Python module that serves it, so the client is package data
    // rather than a sibling directory the server has to go looking for. A
    // release archive has no repository to find `apps/web/dist` in.
    outDir: "src/engine/apps/web/client",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    // The component tier only. `e2e/` holds Playwright specs, which Vitest's
    // default pattern would otherwise collect and run without a browser.
    include: ["src/**/*.test.{ts,tsx}"],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
