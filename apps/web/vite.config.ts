import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { apiProxy } from "./src/api-proxy";

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
    // Which prefixes, and why each is forwarded the way it is, is
    // `src/api-proxy.ts` -- a module a test can read, unlike this file.
    proxy: apiProxy(process.env),
  },
});
