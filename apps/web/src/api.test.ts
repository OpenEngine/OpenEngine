import { afterEach, describe, expect, it, vi } from "vitest";

import {
  answerQuestion,
  api,
  createProject,
  messageText,
  newChatAgent,
  setThreadAutoApprove,
  type EngineConfig,
} from "./api";

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

describe("newChatAgent", () => {
  const config = {
    agents: [],
    runners: [],
    defaultAgent: "coder",
    planAgent: "planner",
    defaultRunner: "claude",
    workflowRunners: [],
    defaultWorkflowRunner: "claude",
    workflows: [],
  } satisfies EngineConfig;

  it("starts a plan on the planning agent and a chat on the default", () => {
    expect(newChatAgent(config, true)).toBe("planner");
    expect(newChatAgent(config, false)).toBe("coder");
  });

  /** The id is the server's to name, so a deployment composing no planner says
   *  so with an empty one -- and the page still opens on something. */
  it("falls back to the default agent when no planner is composed", () => {
    expect(newChatAgent({ ...config, planAgent: "" }, true)).toBe("coder");
  });
});

describe("setThreadAutoApprove", () => {
  it("updates the conversation setting", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "thread-1", autoApprove: true }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);

    await setThreadAutoApprove("thread-1", true);

    expect(fetch).toHaveBeenCalledWith(
      "/api/threads/thread-1",
      expect.objectContaining({ method: "PATCH", body: '{"autoApprove":true}' }),
    );
  });
});

describe("createProject", () => {
  it("creates a project with the generated name", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ projectId: "project-1", name: "Engine roadmap" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);

    await expect(createProject("Engine roadmap")).resolves.toEqual({
      projectId: "project-1",
      name: "Engine roadmap",
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({ method: "POST", body: '{"name":"Engine roadmap"}' }),
    );
  });
});

describe("answerQuestion", () => {
  it("submits structured answers to the waiting interaction", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ approval: { id: "approval-1" } }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);

    await answerQuestion("thread-1", "approval-1", { api: ["Public"] });

    expect(fetch).toHaveBeenCalledWith(
      "/api/threads/thread-1/runs/current/approvals/approval-1",
      expect.objectContaining({ method: "POST", body: '{"answers":{"api":["Public"]}}' }),
    );
  });
});
