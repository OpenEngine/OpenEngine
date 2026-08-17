import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  QueueItemPrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import {
  api,
  attachWorkspace,
  detachWorkspace,
  messageText,
  RUN_NOT_STARTED_ERROR_CODE,
  type ApiThread,
} from "./api";

const COMPOSER_DRAFT_KEY_PREFIX = "engine.composerDraft.";
const NEW_CHAT_DRAFT_ID = "new";

function toolResultText(result: unknown): string {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2) ?? String(result);
  } catch {
    return String(result);
  }
}

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
            {part.result !== undefined && (
              <pre className="tool-result">{toolResultText(part.result)}</pre>
            )}
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

/** What a failed run has to say for itself.
 *
 *  assistant-ui does not keep the thrown Error: `toAssistantError` normalizes
 *  it to a plain `{code, message}`, so an `instanceof Error` test never matches
 *  and stringifying the object prints "[object Object]" over the one sentence
 *  the reader needed. Read the message wherever it ended up.
 */
function errorText(error: unknown): string {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && "message" in error) {
    const { message } = error as { message: unknown };
    if (typeof message === "string") return message;
  }
  return "The agent run failed.";
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
            <Ticked text={errorText(error)} />
          </p>
        </MessagePrimitive.Error>
      </div>
    </MessagePrimitive.Root>
  );
}

function Composer() {
  const aui = useAui();
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const canSend = useAuiState((state) => state.composer.canSend);
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const text = useAuiState((state) => state.composer.text);
  const messages = useAuiState((state) => state.thread.messages);
  const draftKey = `${COMPOSER_DRAFT_KEY_PREFIX}${remoteId ?? NEW_CHAT_DRAFT_ID}`;
  const [restoredDraftKey, setRestoredDraftKey] = useState<string>();
  const restoredFailure = useRef<string | undefined>(undefined);

  useEffect(() => {
    const savedDraft = window.localStorage.getItem(draftKey) ?? "";
    if (aui.composer.getState().text !== savedDraft) {
      aui.composer.setText(savedDraft);
    }
    setRestoredDraftKey(draftKey);
  }, [aui, draftKey]);

  useEffect(() => {
    if (restoredDraftKey !== draftKey) return;
    if (text) window.localStorage.setItem(draftKey, text);
    else window.localStorage.removeItem(draftKey);
  }, [draftKey, restoredDraftKey, text]);

  useEffect(() => {
    // A pre-stream failure means the server never stored this user message,
    // so put it back where it can be corrected or sent again.
    const failed = messages.at(-1);
    const submitted = messages.at(-2);
    if (
      failed?.role !== "assistant" ||
      failed.status.type !== "incomplete" ||
      failed.status.reason !== "error" ||
      !failed.status.error ||
      typeof failed.status.error !== "object" ||
      !("code" in failed.status.error) ||
      failed.status.error.code !== RUN_NOT_STARTED_ERROR_CODE ||
      submitted?.role !== "user"
    ) {
      return;
    }

    const failureKey = `${draftKey}:${failed.id}`;
    if (restoredFailure.current === failureKey) return;
    restoredFailure.current = failureKey;

    const submittedText = messageText(submitted);
    if (submittedText && !aui.composer.getState().text) {
      aui.composer.setText(submittedText);
    }
  }, [aui, draftKey, messages]);

  const send = () => {
    aui.composer.send();
    window.localStorage.removeItem(draftKey);
  };

  const queueOnEnter = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      !isRunning ||
      !canSend ||
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing
    ) {
      return;
    }
    event.preventDefault();
    send();
  };

  return (
    <div className="composer-stack">
      <div className="message-queue" aria-live="polite">
        <ComposerPrimitive.Queue>
          {() => (
            <div className="queued-message">
              <span className="queued-label">Queued</span>
              <QueueItemPrimitive.Text className="queued-text" />
              <QueueItemPrimitive.Remove
                className="queued-remove"
                aria-label="Remove queued message"
                title="Remove queued message"
              >
                ×
              </QueueItemPrimitive.Remove>
            </div>
          )}
        </ComposerPrimitive.Queue>
      </div>
      <ComposerPrimitive.Root
        className="composer"
        onSubmit={() => window.localStorage.removeItem(draftKey)}
      >
        <ComposerPrimitive.Input
          className="composer-input"
          placeholder={
            isRunning
              ? "Queue a message for when the agent is done…"
              : "Ask the agent about this repository…"
          }
          aria-label="Message the agent"
          rows={1}
          onKeyDown={queueOnEnter}
        />
        <ComposerPrimitive.Cancel
          className="composer-button composer-cancel"
          onClick={(event) => {
            const { remoteId } = aui.threadListItem.getState();
            if (!remoteId) return;

            const queued = aui.composer.getState().queue[0];
            if (!queued) {
              void fetch(`/api/threads/${remoteId}/runs/current`, { method: "DELETE" });
              return;
            }

            // Let the server finish cancelling before starting the follow-up.
            // Preventing the primitive's local cancel lets the queue drain as
            // soon as the active stream closes.
            event.preventDefault();
            void fetch(`/api/threads/${remoteId}/runs/current`, { method: "DELETE" }).then(
              () => {
                if (aui.composer.getState().queue.some((item) => item.id === queued.id)) {
                  aui.composer.queueItem({ id: queued.id }).move({ lane: "steer" });
                }
              },
            );
          }}
        >
          Stop
        </ComposerPrimitive.Cancel>
        <button
          type="button"
          className="composer-button"
          disabled={!canSend}
          onClick={send}
        >
          {isRunning ? "Queue" : "Send"}
        </button>
      </ComposerPrimitive.Root>
    </div>
  );
}

type WorkspaceCustom = {
  workspaceRoot?: string;
  workspaceRef?: string;
  workspaceAttached?: boolean;
  workflowRunId?: string;
  workflowStepId?: string;
};

function WorkflowBacklink() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  if (!custom?.workflowRunId) return null;
  return (
    <a className="workflow-backlink" href={`/runs/${custom.workflowRunId}`}>
      ← Back to run <code>{custom.workflowRunId}</code>
      {custom.workflowStepId && <> · {custom.workflowStepId} step</>}
    </a>
  );
}

/** Server messages mark the command you are meant to run in backticks; show
 *  that span as the code it is, so it can be read and copied as one thing. */
function Ticked({ text }: { text: string }) {
  return (
    <>
      {text.split(/`([^`]+)`/).map((part, index) =>
        index % 2 ? <code key={index}>{part}</code> : part,
      )}
    </>
  );
}

/** This chat's worktree: where it is, how to read its work, and a way to
 *  hand the directory back or ask for it again. */
function WorkspaceTagline() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const [fetched, setFetched] = useState<ApiThread>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    setFetched(undefined);
    setError(undefined);
    if (!remoteId) return;
    let current = true;
    void api<ApiThread>(`/api/threads/${remoteId}`)
      .then((thread) => {
        if (current) setFetched(thread);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [remoteId]);

  if (!remoteId) return null;

  const workspace: WorkspaceCustom = fetched ?? custom ?? {};
  const attached = workspace.workspaceAttached ?? Boolean(workspace.workspaceRoot);

  async function toggle() {
    if (!remoteId) return;
    setBusy(true);
    setError(undefined);
    try {
      setFetched(await (attached ? detachWorkspace : attachWorkspace)(remoteId));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workspace-tagline">
      <p>
        {attached ? (
          <>
            Working in <code>cd {workspace.workspaceRoot}</code>
          </>
        ) : workspace.workspaceRef ? (
          <>
            Detached — the work is on <code>git checkout {workspace.workspaceRef}</code>
          </>
        ) : (
          <>No worktree. Attach one to give this chat somewhere to work.</>
        )}
      </p>
      <button
        type="button"
        className="workspace-button"
        onClick={() => void toggle()}
        disabled={busy}
      >
        {busy
          ? "Working…"
          : attached
            ? "Detach"
            : workspace.workspaceRef
              ? "Reattach"
              : "Attach"}
      </button>
      {error && (
        <p className="workspace-error">
          <Ticked text={error} />
        </p>
      )}
    </div>
  );
}

export function ChatThread() {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <WorkflowBacklink />
        <div className="welcome">
          <span className="eyebrow">OPENENGINE / CHAT</span>
          <h1>Start a conversation.</h1>
          <p>Each chat has its own agent history and Git worktree.</p>
        </div>
        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <UserMessage /> : <AssistantMessage />
          }
        </ThreadPrimitive.Messages>
        <ConversationFooter />
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

function ConversationFooter() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  const workflowConversation = Boolean(custom?.workflowRunId);
  return (
    <ThreadPrimitive.ViewportFooter className="thread-footer">
      <ThreadPrimitive.ScrollToBottom className="scroll-button">
        Jump to latest
      </ThreadPrimitive.ScrollToBottom>
      {workflowConversation ? (
        <p className="workflow-conversation-note">
          This transcript belongs to a workflow step. Return to the run for status and actions.
        </p>
      ) : (
        <>
          <Composer />
          {/* Under the composer rather than in the welcome header: a detached
              chat refuses to run, so the way to fix that cannot be somewhere
              you have to scroll a long conversation to reach. */}
          <WorkspaceTagline />
          <p className="composer-note">Runs are read-only in this chat's isolated worktree.</p>
        </>
      )}
    </ThreadPrimitive.ViewportFooter>
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
        <ThreadListItemPrimitive.Unarchive
          className="thread-action restore-action"
          aria-label="Restore chat"
          title="Restore chat"
        >
          Restore
        </ThreadListItemPrimitive.Unarchive>
      ) : (
        <ThreadListItemPrimitive.Archive
          className="thread-action danger"
          aria-label="Archive chat"
          title="Archive chat"
        >
          ×
        </ThreadListItemPrimitive.Archive>
      )}
    </ThreadListItemPrimitive.Root>
  );
}

export function ChatSidebar() {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">e</span>
        <span>openengine</span>
      </div>
      <a className="run-nav-link run-nav-secondary chat-run-link" href="/runs">Workflow runs</a>
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
        <span className="status-dot" /> Local openengine
      </div>
    </aside>
  );
}
