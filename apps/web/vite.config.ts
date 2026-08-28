import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { apiProxyTarget } from "./src/api-proxy";

export default defineConfig({
  plugins: [react()],
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
      "/api": apiProxyTarget(process.env),
    },
  },
});
