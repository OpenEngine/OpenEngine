import { describe, expect, it, vi } from "vitest";

import type { ApiApproval } from "./api";
import { readApprovals } from "./approvals";
import { readRunResponse, watchApprovalEvents } from "./runtime";

const approval: ApiApproval = {
  id: "approval-1",
  status: "pending",
  kind: "command_execution",
  reason: "Needs access",
  command: "git status",
  cwd: "/repo",
  toolName: null,
  toolCallId: null,
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

describe("watchApprovalEvents", () => {
  it("publishes pushed snapshots at the current end of the open conversation", () => {
    const threadId = "thread:open";
    let messageIndex = 2;
    let closed = false;
    let opened = "";
    const connection = {
      onmessage: null as ((event: MessageEvent<string>) => void) | null,
      close() {
        closed = true;
      },
    };
    const stop = watchApprovalEvents(
      threadId,
      () => messageIndex,
      (url) => {
        opened = url;
        return connection;
      },
    );

    messageIndex = 4;
    connection.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(approval) }),
    );

    expect(opened).toBe("/api/threads/thread%3Aopen/approval-events");
    expect(readApprovals(threadId)).toEqual([{ approval, messageIndex: 4 }]);
    expect(closed).toBe(false);

    stop();
    expect(closed).toBe(true);
  });

  it("leaves the feed connected after a malformed event", () => {
    const report = vi.spyOn(console, "error").mockImplementation(() => {});
    let closed = false;
    const connection = {
      onmessage: null as ((event: MessageEvent<string>) => void) | null,
      close() {
        closed = true;
      },
    };
    const stop = watchApprovalEvents("thread-errors", () => 0, () => connection);

    connection.onmessage?.(new MessageEvent("message", { data: "{" }));

    expect(report).toHaveBeenCalledOnce();
    expect(closed).toBe(false);
    stop();
    report.mockRestore();
  });
});
