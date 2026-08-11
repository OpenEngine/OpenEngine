import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import { useEffect, useState } from "react";

import { api, type ApiThread } from "./api";

function TextParts() {
  return (
    <MessagePrimitive.Parts>
      {({ part }) =>
        part.type === "text" ? (
          <MessagePartPrimitive.Text component="p" />
        ) : part.type === "tool-call" ? (
          <details className="tool-call">
            <summary>{part.status.type === "running" ? "running" : "ran"} {part.toolName}</summary>
            <pre>{part.argsText || JSON.stringify(part.args, null, 2)}</pre>
            {part.result !== undefined && <pre className="tool-result">{String(part.result)}</pre>}
          </details>
        ) : null
      }
    </MessagePrimitive.Parts>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message message-user">
      <TextParts />
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  const error = useAuiState((state) => {
    const status = state.message.status;
    if (status?.type !== "incomplete" || status.reason !== "error") return undefined;
    return status.error;
  });

  return (
    <MessagePrimitive.Root className="message message-assistant">
      <div className="assistant-mark">e</div>
      <div className="assistant-content">
        <TextParts />
        <MessagePrimitive.Error>
          <p className="message-error">
            {error instanceof Error ? error.message : String(error ?? "The agent run failed.")}
          </p>
        </MessagePrimitive.Error>
      </div>
    </MessagePrimitive.Root>
  );
}

function Composer() {
  const aui = useAui();

  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Ask the agent about this repository…"
        aria-label="Message the agent"
        rows={1}
      />
      <ComposerPrimitive.Cancel
        className="composer-button composer-cancel"
        onClick={() => {
          const { remoteId } = aui.threadListItem.getState();
          if (remoteId) {
            void fetch(`/api/threads/${remoteId}/runs/current`, { method: "DELETE" });
          }
        }}
      >
        Stop
      </ComposerPrimitive.Cancel>
      <ComposerPrimitive.Send className="composer-button">Send</ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}

function useCurrentWorkspaceRoot() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | { workspaceRoot?: string }
    | undefined;
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const [fetchedRoot, setFetchedRoot] = useState<string>();

  useEffect(() => {
    setFetchedRoot(undefined);
    if (custom?.workspaceRoot || !remoteId) return;
    let current = true;
    void api<ApiThread>(`/api/threads/${remoteId}`).then((thread) => {
      if (current) setFetchedRoot(thread.workspaceRoot);
    });
    return () => {
      current = false;
    };
  }, [custom?.workspaceRoot, remoteId]);

  return custom?.workspaceRoot ?? fetchedRoot;
}

function WorkspaceTagline({ workspaceRoot }: { workspaceRoot?: string }) {
  if (!workspaceRoot) return null;

  return (
    <p className="workspace-tagline">
      Checkout worktree <code>cd {workspaceRoot}</code>
    </p>
  );
}

export function ChatThread() {
  const workspaceRoot = useCurrentWorkspaceRoot();

  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <div className="welcome">
          <span className="eyebrow">ENGINE / CHAT</span>
          <h1>Start a conversation.</h1>
          <p>Each chat has its own agent history and Git worktree.</p>
          <WorkspaceTagline workspaceRoot={workspaceRoot} />
        </div>
        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <UserMessage /> : <AssistantMessage />
          }
        </ThreadPrimitive.Messages>
        <ThreadPrimitive.ViewportFooter className="thread-footer">
          <ThreadPrimitive.ScrollToBottom className="scroll-button">
            Jump to latest
          </ThreadPrimitive.ScrollToBottom>
          <Composer />
          <div className="thread-footer-meta">
            <span>Runs are read-only in this chat's isolated worktree.</span>
            {workspaceRoot && (
              <span className="footer-workspace" title={workspaceRoot}>
                Workspace <code>{workspaceRoot}</code>
              </span>
            )}
          </div>
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

function ThreadItemMeta() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | { agentId?: string; runner?: string; workspaceRoot?: string }
    | undefined;
  const isRunning = useAuiState((state) => state.threadListItem.isRunning);
  return (
    <span className="thread-meta">
      {isRunning && <span className="running-dot" aria-label="Agent is running" />}
      {[custom?.agentId, custom?.runner].filter(Boolean).join(" · ")}
    </span>
  );
}

function ThreadListItem({ archived = false }: { archived?: boolean }) {
  return (
    <ThreadListItemPrimitive.Root className="thread-item">
      {archived ? (
        <div className="thread-trigger">
          <span className="thread-copy">
            <ThreadListItemPrimitive.Title fallback="New chat" />
            <ThreadItemMeta />
          </span>
        </div>
      ) : (
        <ThreadListItemPrimitive.Trigger className="thread-trigger">
          <span className="thread-copy">
            <ThreadListItemPrimitive.Title fallback="New chat" />
            <ThreadItemMeta />
          </span>
        </ThreadListItemPrimitive.Trigger>
      )}
      {archived ? (
        <ThreadListItemPrimitive.Unarchive className="thread-action" aria-label="Restore chat">
          ↗
        </ThreadListItemPrimitive.Unarchive>
      ) : (
        <ThreadListItemPrimitive.Archive className="thread-action" aria-label="Archive chat">
          —
        </ThreadListItemPrimitive.Archive>
      )}
      <ThreadListItemPrimitive.Delete className="thread-action danger" aria-label="Delete chat">
        ×
      </ThreadListItemPrimitive.Delete>
    </ThreadListItemPrimitive.Root>
  );
}

export function ChatSidebar() {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">e</span>
        <span>engine</span>
      </div>
      <ThreadListPrimitive.Root className="thread-list">
        <ThreadListPrimitive.New className="new-thread">+ New chat</ThreadListPrimitive.New>
        <div className="thread-list-label">Conversations</div>
        <ThreadListPrimitive.Items>
          {() => <ThreadListItem />}
        </ThreadListPrimitive.Items>
        <details className="archived-list">
          <summary>Archived</summary>
          <ThreadListPrimitive.Items archived>
            {() => <ThreadListItem archived />}
          </ThreadListPrimitive.Items>
        </details>
      </ThreadListPrimitive.Root>
      <div className="sidebar-foot">
        <span className="status-dot" /> Local engine
      </div>
    </aside>
  );
}
