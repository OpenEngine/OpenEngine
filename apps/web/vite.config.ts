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
    // `tailscale serve` reaches this dev server under the machine's tailnet
    // name, which Vite's host check rejects by default. The leading dot admits
    // any `*.ts.net` host rather than pinning one machine's.
    allowedHosts: [".ts.net"],
    proxy: {
      // `changeOrigin: false` keeps the browser's Host header, which Vite
      // otherwise rewrites to the proxy target. The API's CSRF guard accepts
      // an Origin only when it matches localhost or the request's own host, so
      // a rewritten Host turns every mutating call made under the tailnet name
      // into a 403.
      "/api": { target: apiProxyTarget(process.env), changeOrigin: false },
    },
  },
});
