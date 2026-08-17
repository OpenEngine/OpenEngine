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
  decideApproval,
  detachWorkspace,
  messageText,
  RUN_NOT_STARTED_ERROR_CODE,
  stopRun,
  type ApiApproval,
  type ApiThread,
  type ApprovalDecision,
} from "./api";
import { useApproval } from "./approvals";

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

            // Stopping a run that is waiting on an approval goes through the
            // same cancel the card's own button does: the server records the
            // request as cancelled and hands that to the provider, so the
            // action does not run, and only then tears the turn down.
            const stop = () => stopRun(remoteId).catch(() => {});

            const queued = aui.composer.getState().queue[0];
            if (!queued) {
              void stop();
              return;
            }

            // Let the server finish cancelling before starting the follow-up.
            // Preventing the primitive's local cancel lets the queue drain as
            // soon as the active stream closes.
            event.preventDefault();
            void stop().then(() => {
              if (aui.composer.getState().queue.some((item) => item.id === queued.id)) {
                aui.composer.queueItem({ id: queued.id }).move({ lane: "steer" });
              }
            });
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

const DECISION_LABELS: Record<ApprovalDecision, string> = {
  accept: "Approve",
  accept_for_session: "Allow similar actions for this session",
  cancel: "Cancel",
};

const KIND_LABELS: Record<ApiApproval["kind"], string> = {
  command_execution: "Wants to run a command",
  file_change: "Wants to change files",
  tool_use: "Wants to use a tool",
};

/** What became of a request that is no longer open, and on whose say-so. */
function outcomeText(approval: ApiApproval): string {
  if (approval.status === "interrupted")
    return "Interrupted — the agent that asked this is gone, so it can no longer be answered.";
  if (approval.decisionSource === "session_grant")
    return "Approved automatically: you allowed this exact action for this conversation earlier.";
  switch (approval.decision) {
    case "accept":
      return "Approved.";
    case "accept_for_session":
      return "Approved, and allowed again for this conversation without asking.";
    case "cancel":
      return "Cancelled — the action did not run.";
    default:
      return "This request is no longer open.";
  }
}

/** The request's own arguments, shown as fields when they are fields.
 *
 *  Structured rather than dumped: the point of the card is that somebody can
 *  tell what they are agreeing to, and a wall of JSON is read by nobody. What
 *  will not parse is still shown -- unreadable is better than hidden. */
function ApprovalArguments({ approval }: { approval: ApiApproval }) {
  if (!approval.arguments) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(approval.arguments);
  } catch {
    return <pre className="approval-arguments">{approval.arguments}</pre>;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    return <pre className="approval-arguments">{JSON.stringify(parsed, null, 2)}</pre>;

  const fields = Object.entries(parsed as Record<string, unknown>).filter(
    ([key, value]) =>
      value !== null &&
      value !== undefined &&
      // The command already has a line of its own above.
      !(key === "command" && value === approval.command),
  );
  if (!fields.length) return null;

  return (
    <dl className="approval-arguments">
      {fields.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** What the paused turn is asking, and the answers this request permits.
 *
 *  Only those answers: a provider that never offered a session grant cannot
 *  honour one, so offering the button would be offering something we would
 *  have to refuse. */
function ApprovalCard() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const approval = useApproval(remoteId);
  const [submitted, setSubmitted] = useState<ApprovalDecision>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    // A new request is a new question; nothing about the last one carries over.
    setSubmitted(undefined);
    setError(undefined);
  }, [approval?.id]);

  if (!remoteId || !approval) return null;
  const threadId = remoteId;
  const request = approval;
  const pending = request.status === "pending";

  async function decide(decision: ApprovalDecision) {
    setSubmitted(decision);
    setError(undefined);
    try {
      await decideApproval(threadId, request.id, decision);
    } catch (failure) {
      // Stale, already answered, or a provider that has since died. The
      // decision did not land, so the controls come back with the reason.
      setSubmitted(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <section className="approval-card" aria-live="polite">
      <header className="approval-head">
        <span className="approval-kind">{KIND_LABELS[request.kind]}</span>
        <span className="approval-id">{request.id}</span>
      </header>
      {request.reason && <p className="approval-reason">{request.reason}</p>}
      {request.command && <pre className="approval-command">{request.command}</pre>}
      {(request.toolName || request.cwd) && (
        <dl className="approval-facts">
          {request.toolName && (
            <div>
              <dt>Tool</dt>
              <dd>{request.toolName}</dd>
            </div>
          )}
          {request.cwd && (
            <div>
              <dt>Working directory</dt>
              <dd>
                <code>{request.cwd}</code>
              </dd>
            </div>
          )}
        </dl>
      )}
      <ApprovalArguments approval={request} />
      {pending ? (
        <div className="approval-actions">
          {request.allowedDecisions.map((decision) => (
            <button
              key={decision}
              type="button"
              className={`approval-button approval-${decision}`}
              // Disabled the instant one is chosen: a second click is a second
              // decision, and the server refuses those rather than applying
              // them to whatever is running by then.
              disabled={submitted !== undefined}
              onClick={() => void decide(decision)}
            >
              {submitted === decision ? "Sending…" : DECISION_LABELS[decision]}
            </button>
          ))}
        </div>
      ) : (
        <p className="approval-outcome">{outcomeText(request)}</p>
      )}
      {error && (
        <p className="approval-error">
          <Ticked text={error} />
        </p>
      )}
    </section>
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
          {/* Above the composer: a paused turn is waiting on this and on
              nothing else, so it should be the thing under your eyes rather
              than something the message box competes with. */}
          <ApprovalCard />
          <Composer />
          {/* Under the composer rather than in the welcome header: a detached
              chat refuses to run, so the way to fix that cannot be somewhere
              you have to scroll a long conversation to reach. */}
          <WorkspaceTagline />
          <p className="composer-note">
            The codex and claude runners only read. The approval runners can change this
            chat's worktree, and stop to ask first.
          </p>
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
