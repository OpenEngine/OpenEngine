import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiApproval } from "./api";
import { readApprovals } from "./approvals";
import { keepApprovalsFresh, readRunResponse } from "./runtime";

const approval: ApiApproval = {
  id: "approval-1",
  status: "pending",
  kind: "command_execution",
  reason: "Needs access",
  command: "git status",
  cwd: "/repo",
  toolName: null,
  arguments: null,
  allowedDecisions: ["accept", "cancel"],
  decision: null,
  decisionSource: null,
};

function streamingResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  );
}

async function readAll(response: Response, threadId = "thread-runtime") {
  const frames = [];
  for await (const frame of readRunResponse(response, threadId, 4)) frames.push(frame);
  return frames;
}

describe("readRunResponse", () => {
  it("reassembles NDJSON split across chunks and yields complete content frames", async () => {
    const content = JSON.stringify({
      type: "content",
      content: [{ type: "text", text: "whole" }],
    });
    const done = JSON.stringify({ type: "done", content: [] });

    await expect(
      readAll(streamingResponse([content.slice(0, 19), `${content.slice(19)}\n${done}\n`])),
    ).resolves.toEqual([
      { content: [{ type: "text", text: "whole" }] },
      { content: [] },
    ]);
  });

  it("publishes approvals without yielding a content frame", async () => {
    const threadId = "thread-approval-event";
    const response = streamingResponse([
      `${JSON.stringify({ type: "approval", approval })}\n`,
    ]);

    await expect(readAll(response, threadId)).resolves.toEqual([]);
    expect(readApprovals(threadId)).toEqual([{ approval, messageIndex: 4 }]);
  });

  it("throws streamed errors", async () => {
    const response = streamingResponse([
      `${JSON.stringify({ type: "error", error: "provider stopped" })}\n`,
    ]);

    await expect(readAll(response)).rejects.toThrow("provider stopped");
  });

  it("returns no frames for a 204 response", async () => {
    await expect(readAll(new Response(null, { status: 204 }))).resolves.toEqual([]);
  });
});

describe("keepApprovalsFresh", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("refreshes approvals while the same conversation remains open", async () => {
    vi.useFakeTimers();
    const refresh = vi.fn(async () => {});
    const stop = keepApprovalsFresh("thread-open", refresh);

    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenLastCalledWith("thread-open");

    await vi.advanceTimersByTimeAsync(2_000);
    expect(refresh).toHaveBeenCalledTimes(3);

    stop();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it("keeps polling after a transient refresh failure", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const refresh = vi
      .fn<(threadId: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);
    const stop = keepApprovalsFresh("thread-retry", refresh);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(refresh).toHaveBeenCalledTimes(2);

    stop();
  });
});
