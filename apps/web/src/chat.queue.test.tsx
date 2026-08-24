import { act, render, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Composer, QueuedMessagePersistence } from "./chat";

const runtime = vi.hoisted(() => ({
  draft: "",
  sending: false,
  state: {
    threadListItem: { remoteId: "thread-queue" as string | undefined },
    thread: { isLoading: false, isRunning: true, messages: [] },
    composer: {
      canSend: false,
      text: "",
      queue: [] as Array<{
        id: string;
        parts: Array<{ type: "text"; text: string }>;
      }>,
    },
  },
  send: vi.fn(),
  setText: vi.fn(),
  listeners: new Set<() => void>(),
  subscribe(callback: () => void) {
    runtime.listeners.add(callback);
    return () => runtime.listeners.delete(callback);
  },
  notify() {
    for (const callback of runtime.listeners) callback();
  },
}));

vi.mock("@assistant-ui/react", async () => {
  const { useSyncExternalStore } = await import("react");
  const Root = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const aui = {
    composer: {
      getState: () => ({
        text: runtime.draft,
        queue: runtime.state.composer.queue,
        canSend: runtime.state.composer.canSend,
      }),
      setText: runtime.setText,
      send: runtime.send,
    },
    threadListItem: { getState: () => runtime.state.threadListItem },
    queueItem: () => ({ move: vi.fn() }),
  };
  return {
    ComposerPrimitive: {
      Root,
      Input: () => <textarea aria-label="Message the agent" />,
      Cancel: Root,
      Queue: () => null,
    },
    QueueItemPrimitive: { Text: () => null, Remove: Root },
    useAuiState: (select: (state: typeof runtime.state) => unknown) =>
      useSyncExternalStore(
        runtime.subscribe,
        () => select(runtime.state),
        () => select(runtime.state),
      ),
    useAui: () => aui,
  };
});

const queueKey = "engine.composerQueue.thread-queue";

beforeEach(() => {
  window.localStorage.clear();
  runtime.draft = "";
  runtime.sending = false;
  runtime.state.threadListItem.remoteId = "thread-queue";
  runtime.state.thread.isLoading = false;
  runtime.state.composer.canSend = false;
  runtime.state.composer.text = "";
  runtime.state.composer.queue = [];
  runtime.listeners.clear();
  runtime.setText.mockReset().mockImplementation((text: string) => {
    runtime.draft = text;
    runtime.state.composer.text = text;
    runtime.state.composer.canSend = text.length > 0 && !runtime.sending;
    runtime.notify();
  });
  runtime.send.mockReset().mockImplementation(() => {
    const text = runtime.draft;
    runtime.sending = true;
    runtime.state.composer.canSend = false;
    runtime.state.composer.queue.push({
      id: `queued-${runtime.state.composer.queue.length}`,
      parts: [{ type: "text", text }],
    });
    runtime.draft = "";
    runtime.state.composer.text = "";
    runtime.notify();
    queueMicrotask(() => {
      runtime.sending = false;
      runtime.state.composer.canSend = runtime.draft.length > 0;
      runtime.notify();
    });
  });
});

afterEach(() => vi.clearAllMocks());

describe("QueuedMessagePersistence", () => {
  it("restores queued follow-ups after history has loaded and preserves the draft", async () => {
    runtime.state.thread.isLoading = true;
    window.localStorage.setItem("engine.composerDraft.thread-queue", "unfinished draft");
    window.localStorage.setItem(queueKey, JSON.stringify(["first follow-up", "second follow-up"]));
    render(<Composer />);

    expect(runtime.send).not.toHaveBeenCalled();

    runtime.state.thread.isLoading = false;
    act(() => runtime.notify());

    await waitFor(() => expect(runtime.send).toHaveBeenCalledTimes(2));
    expect(runtime.setText.mock.calls.map(([text]) => text)).toEqual([
      "unfinished draft",
      "first follow-up",
      "second follow-up",
      "unfinished draft",
    ]);
    expect(runtime.draft).toBe("unfinished draft");
    expect(JSON.parse(window.localStorage.getItem(queueKey) ?? "[]")).toEqual([
      "first follow-up",
      "second follow-up",
    ]);
  });

  it("updates durable queue state when a pending item is added or removed", async () => {
    render(<QueuedMessagePersistence draftRestored />);

    runtime.state.composer.queue = [
      { id: "queued-1", parts: [{ type: "text", text: "keep me" }] },
    ];
    act(() => runtime.notify());
    await waitFor(() =>
      expect(window.localStorage.getItem(queueKey)).toBe('["keep me"]'),
    );

    runtime.state.composer.queue = [];
    act(() => runtime.notify());
    await waitFor(() => expect(window.localStorage.getItem(queueKey)).toBeNull());
  });

  it("ignores malformed saved queue data", async () => {
    window.localStorage.setItem(queueKey, "not json");
    render(<QueuedMessagePersistence draftRestored />);

    await waitFor(() => expect(window.localStorage.getItem(queueKey)).toBeNull());
    expect(runtime.send).not.toHaveBeenCalled();
  });
});
