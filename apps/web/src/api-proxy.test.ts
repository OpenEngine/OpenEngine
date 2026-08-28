import { describe, expect, it } from "vitest";

import { DEFAULT_API_URL, apiProxyTarget } from "./api-proxy";

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
