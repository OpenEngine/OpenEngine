import { describe, expect, it } from "vitest";

import { DEFAULT_API_URL, apiProxy, apiProxyTarget } from "./api-proxy";

describe("apiProxyTarget", () => {
  it("follows the API the development server actually started", () => {
    expect(apiProxyTarget({ ENGINE_API_URL: "http://localhost:8123" })).toBe(
      "http://localhost:8123",
    );
  });

  it("defaults to the port engine-web serves on, so nothing is needed by hand", () => {
    expect(apiProxyTarget({})).toBe(DEFAULT_API_URL);
    expect(apiProxyTarget()).toBe(DEFAULT_API_URL);
  });

  it("treats an empty variable as unset rather than as an address", () => {
    // An exported-but-empty variable is a proxy pointed at nothing, which
    // fails as a browser error rather than as a message anybody can act on.
    expect(apiProxyTarget({ ENGINE_API_URL: "  " })).toBe(DEFAULT_API_URL);
  });
});

describe("apiProxy", () => {
  it("forwards every prefix the API serves, not only its own", () => {
    // `/graph` is the graph engine's control surface, which the `[BETA]`
    // WorkOrder page reads. Left out, Vite answers it with `index.html` and a
    // 200, and the page reports a JSON parse error instead of showing the run.
    expect(Object.keys(apiProxy({ ENGINE_API_URL: "http://localhost:8123" }))).toEqual([
      "/api",
      "/graph",
    ]);
  });

  it("sends every prefix to the API the development server started", () => {
    const proxy = apiProxy({ ENGINE_API_URL: "http://localhost:8123" });
    for (const options of Object.values(proxy)) {
      expect(options.target).toBe("http://localhost:8123");
      // Rewriting the Host header is what turns a mutating call made under a
      // tailnet name into a 403.
      expect(options.changeOrigin).toBe(false);
    }
  });
});
