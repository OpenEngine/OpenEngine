import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ApiApproval } from "./api";
import { publishApproval, readApprovals, useApprovals } from "./approvals";

function approval(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return {
    id: "approval-1",
    status: "pending",
    kind: "tool_use",
    reason: null,
    command: null,
    cwd: null,
    toolName: "search",
    arguments: null,
    allowedDecisions: ["accept", "cancel"],
    decision: null,
    decisionSource: null,
    ...overrides,
  };
}

describe("approval snapshots", () => {
  it("returns one stable empty snapshot for empty threads", () => {
    expect(readApprovals("empty-a")).toBe(readApprovals("empty-a"));
    expect(readApprovals("empty-a")).toBe(readApprovals("empty-b"));
  });

  it("keeps threads isolated", () => {
    publishApproval("isolated-a", approval({ id: "a" }), 1);
    publishApproval("isolated-b", approval({ id: "b" }), 2);

    expect(readApprovals("isolated-a").map((entry) => entry.approval.id)).toEqual(["a"]);
    expect(readApprovals("isolated-b").map((entry) => entry.approval.id)).toEqual(["b"]);
  });

  it("replaces a decided snapshot without moving its original message", () => {
    const threadId = "replacement";
    publishApproval(threadId, approval(), 3);
    publishApproval(
      threadId,
      approval({ status: "decided", decision: "accept", decisionSource: "user" }),
      99,
    );

    expect(readApprovals(threadId)).toEqual([
      {
        messageIndex: 3,
        approval: approval({
          status: "decided",
          decision: "accept",
          decisionSource: "user",
        }),
      },
    ]);
  });

  it("does not notify subscribers for an unchanged snapshot", () => {
    const threadId = "unchanged";
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useApprovals(threadId);
    });

    act(() => publishApproval(threadId, approval(), 5));
    expect(result.current).toHaveLength(1);
    expect(renders).toBe(2);

    act(() => publishApproval(threadId, { ...approval() }, 8));
    expect(renders).toBe(2);
  });
});
