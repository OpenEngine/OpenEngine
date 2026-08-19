import { describe, expect, it } from "vitest";

import type { ApiApproval } from "./api";
import { readApprovals } from "./approvals";
import { readRunResponse } from "./runtime";

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
