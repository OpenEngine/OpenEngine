import { afterEach, describe, expect, it, vi } from "vitest";

import { api, messageText } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("prefers a JSON error from the server", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "specific failure" }), {
          status: 400,
          statusText: "Bad Request",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api("/broken")).rejects.toThrow("specific failure");
  });

  it("falls back to status text for a non-JSON error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("upstream unavailable", {
          status: 503,
          statusText: "Service Unavailable",
        }),
      ),
    );

    await expect(api("/broken")).rejects.toThrow("503 Service Unavailable");
  });

  it("returns undefined for a 204 response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));

    await expect(api("/empty")).resolves.toBeUndefined();
  });
});

describe("messageText", () => {
  it("returns string content unchanged", () => {
    expect(messageText({ content: "hello" })).toBe("hello");
  });

  it("joins text parts and ignores other content", () => {
    expect(
      messageText({
        content: [
          { type: "text", text: "first" },
          { type: "tool-call", toolName: "bash" },
          { type: "text", text: "second" },
        ],
      }),
    ).toBe("first\nsecond");
  });

  it("returns an empty string for unsupported content", () => {
    expect(messageText({ content: { type: "text", text: "hidden" } })).toBe("");
  });
});
