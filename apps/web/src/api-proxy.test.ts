import { describe, expect, it } from "vitest";

import { DEFAULT_API_URL, PROXIED_PREFIXES, apiProxy, apiProxyTarget } from "./api-proxy";

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
  it("gives every prefix in the list an entry, so listing one is enough", () => {
    // Derived from the constant rather than repeating it: whether the list
    // itself covers what the application serves is not knowable from here,
    // because the routes are Python. `tests/test_web_app.py` compares those two
    // and is what goes red when a prefix is added on one side only.
    expect(Object.keys(apiProxy())).toEqual([...PROXIED_PREFIXES]);
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
