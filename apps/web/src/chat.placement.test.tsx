/** Where in a turn an approval is shown.
 *
 *  The one thing the cards cannot say for themselves: a request rendered
 *  perfectly in the wrong place tells the reader the agent asked about a
 *  command it never asked about. So this reads the rendered order rather than
 *  the filtering behind it, and assistant-ui's message primitives are replaced
 *  by the smallest things that hand one turn's parts to our renderer.
 */

import { render } from "@testing-library/react";
import type { ComponentProps, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiApproval } from "./api";
import { publishApproval } from "./approvals";
import {
  AssistantMessage,
  ToolCallIndex,
  UnanchoredApprovals,
  useToolCallIds,
} from "./chat";

type Part =
  | { type: "text"; text: string }
  | {
      type: "tool-call";
      toolCallId: string;
      toolName: string;
      args: Record<string, unknown>;
      argsText: string;
      status: { type: string };
    };

const turn = vi.hoisted(() => ({
  parts: [] as unknown[],
  message: { index: 0, isLast: true, status: { type: "complete" } },
  threadListItem: { remoteId: "thread-placement" },
  thread: { messages: [] as { content: unknown[] }[] },
}));

vi.mock("@assistant-ui/react", async () => {
  const { createContext, createElement, useContext } = await import("react");
  const PartContext = createContext<Part | null>(null);
  return {
    useAuiState: (select: (state: typeof turn) => unknown) => select(turn),
    useToolCallElapsed: () => undefined,
    MessagePrimitive: {
      Root: ({ children, ...props }: ComponentProps<"div">) => (
        <div {...props}>{children}</div>
      ),
      Error: () => null,
      Parts: ({ children }: { children: (props: { part: unknown }) => ReactNode }) => (
        <>
          {turn.parts.map((part, index) => (
            <PartContext.Provider key={index} value={part as Part}>
              {children({ part })}
            </PartContext.Provider>
          ))}
        </>
      ),
    },
    MessagePartPrimitive: {
      Text: ({ component }: { component: string }) => {
        const part = useContext(PartContext);
        return createElement(component, null, part?.type === "text" ? part.text : "");
      },
    },
  };
});

function approval(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return {
    id: "approval-1",
    status: "decided",
    kind: "command_execution",
    reason: null,
    command: "rm -rf build",
    cwd: "/repo",
    toolName: "Bash",
    toolCallId: null,
    arguments: null,
    allowedDecisions: ["accept", "cancel"],
    decision: "accept",
    decisionSource: "user",
    ...overrides,
  };
}

function call(toolCallId: string, toolName: string, detail: string): Part {
  return {
    type: "tool-call",
    toolCallId,
    toolName,
    args: { command: detail },
    argsText: JSON.stringify({ command: detail }),
    status: { type: "complete" },
  };
}

/** The turn under the thread's call index, which is where `ChatThread` puts it
 *  and where the placement is decided. */
function renderTurn() {
  return render(
    <ToolCallIndex>
      <AssistantMessage />
    </ToolCallIndex>,
  );
}

/** One turn as the reader meets it: every folded row and paragraph, in order. */
function rendered(): string[] {
  return Array.from(
    document.querySelectorAll(".tool-title, .approval-summary, .message-assistant > p"),
  ).map((node) => node.textContent ?? "");
}

/** Snapshots are global and never forgotten, which is the point of them. A
 *  thread of its own per test is how one case stops being the next case's
 *  history. */
let threads = 0;
beforeEach(() => {
  threads += 1;
  turn.threadListItem.remoteId = `thread-placement-${threads}`;
  turn.parts = [];
  turn.thread.messages = [];
});

describe("where a turn shows what it was asked to allow", () => {
  it("puts each request under its own call, and the answer below both", () => {
    const parts = [
      call("call-1", "Bash", "rm -rf build"),
      call("call-2", "Bash", "ls"),
      call("call-3", "Edit", "src/api.ts"),
      { type: "text" as const, text: "Cleaned the tree and fixed the export." },
    ];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    publishApproval(threadId, approval({ toolCallId: "call-1" }), 0);
    publishApproval(
      threadId,
      approval({ id: "approval-2", toolCallId: "call-3", command: null, toolName: "Edit" }),
      0,
    );

    renderTurn();

    expect(rendered()).toEqual([
      "ran Bash",
      "Approved · rm -rf build",
      "ran Bash",
      "ran Edit",
      "Approved · Edit",
      "Cleaned the tree and fixed the export.",
    ]);
  });

  it("keeps a request the transcript cannot place at the end of the turn", () => {
    const parts = [call("call-1", "Bash", "ls"), { type: "text" as const, text: "Done." }];
    turn.parts = parts;
    turn.thread.messages = [{ content: parts }];
    const threadId = turn.threadListItem.remoteId;
    // One naming no call at all, one naming a call this transcript does not
    // contain: both are requests with nothing to sit beside.
    publishApproval(threadId, approval({ id: "approval-3" }), 0);
    publishApproval(
      threadId,
      approval({ id: "approval-4", toolCallId: "call-gone", command: "git push" }),
      0,
    );

    renderTurn();

    expect(rendered()).toEqual([
      "ran Bash",
      "Done.",
      "Approved · rm -rf build",
      "Approved · git push",
    ]);
  });

  it("shows a pushed request before its assistant turn has mounted", () => {
    const threadId = turn.threadListItem.remoteId;
    turn.thread.messages = [{ content: [] }];
    turn.message = { index: 0, isLast: true, status: { type: "complete" } };
    publishApproval(threadId, approval({ status: "pending", decision: null }), 1);

    const view = render(
      <ToolCallIndex>
        <AssistantMessage />
        <UnanchoredApprovals />
      </ToolCallIndex>,
    );

    // The mounted last turn and the live slot must not both claim the request.
    expect(rendered()).toEqual(["Approval needed · rm -rf build"]);

    // Streaming mounted the reply at the index recorded by the event. The
    // card now belongs to that assistant turn instead of the live slot.
    turn.thread.messages = [{ content: [] }, { content: [] }];
    turn.message = { index: 1, isLast: true, status: { type: "complete" } };
    view.rerender(
      <ToolCallIndex>
        <AssistantMessage />
        <UnanchoredApprovals />
      </ToolCallIndex>,
    );
    expect(rendered()).toEqual(["Approval needed · rm -rf build"]);
  });
});

/** A transcript that reports how much of it was read.
 *
 *  What the cost of deciding placement looks like from the outside, and the
 *  only way to see it: a scan run once per turn instead of once renders exactly
 *  the same transcript, and differs only in how much of the conversation is
 *  walked every time a token lands. */
function countingTranscript(turns: number) {
  const reads = { content: 0 };
  const messages = Array.from({ length: turns }, (_, index) => ({
    get content() {
      reads.content += 1;
      return [call(`call-${index}`, "Bash", "ls")];
    },
  }));
  return { reads, messages };
}

describe("what deciding placement costs", () => {
  it("walks the transcript once, however many turns are on screen", () => {
    const alone = countingTranscript(4);
    turn.thread.messages = alone.messages;
    render(
      <ToolCallIndex>
        <AssistantMessage />
      </ToolCallIndex>,
    );

    const crowd = countingTranscript(4);
    turn.thread.messages = crowd.messages;
    render(
      <ToolCallIndex>
        <AssistantMessage />
        <AssistantMessage />
        <AssistantMessage />
        <AssistantMessage />
      </ToolCallIndex>,
    );

    expect(alone.reads.content).toBeGreaterThan(0);
    expect(crowd.reads.content).toBe(alone.reads.content);
  });

  it("hands back the same index when a chunk brings no new call", () => {
    const seen: ReadonlySet<string>[] = [];
    const Probe = () => {
      seen.push(useToolCallIds());
      return null;
    };
    const content = [call("call-1", "Bash", "ls")];
    turn.thread.messages = [{ content }];
    const { rerender } = render(
      <ToolCallIndex>
        <Probe />
      </ToolCallIndex>,
    );

    // What a streamed chunk does: a new array over the calls already placed.
    turn.thread.messages = [{ content }];
    rerender(
      <ToolCallIndex>
        <Probe />
      </ToolCallIndex>,
    );

    expect(seen[1]).toBe(seen[0]);

    // A new call is a new index, because where a request goes has changed.
    turn.thread.messages = [{ content: [...content, call("call-2", "Edit", "api.ts")] }];
    rerender(
      <ToolCallIndex>
        <Probe />
      </ToolCallIndex>,
    );

    expect(seen[2]).not.toBe(seen[0]);
    expect([...seen[2]]).toEqual(["call-1", "call-2"]);
  });

  it("notices a call streamed into the existing transcript array", () => {
    const seen: ReadonlySet<string>[] = [];
    const Probe = () => {
      seen.push(useToolCallIds());
      return null;
    };
    const message = { content: [] as Part[] };
    turn.thread.messages = [message];
    const view = render(
      <ToolCallIndex>
        <Probe />
      </ToolCallIndex>,
    );

    message.content.push(call("call-streamed", "Bash", "git status --short"));
    view.rerender(
      <ToolCallIndex>
        <Probe />
      </ToolCallIndex>,
    );

    expect([...seen.at(-1)!]).toEqual(["call-streamed"]);
  });
});
