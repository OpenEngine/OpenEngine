import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  QueueItemPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useToolCallElapsed,
} from "@assistant-ui/react";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

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
import { useApprovals, type InlineApproval } from "./approvals";
import { Stat, StatStrip } from "./brand";

const COMPOSER_DRAFT_KEY_PREFIX = "engine.composerDraft.";
const NEW_CHAT_DRAFT_ID = "new";

export function toolResultText(result: unknown): string {
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2) ?? String(result);
  } catch {
    return String(result);
  }
}

/** The argument worth putting on the folded line: the thing the call is about.
 *
 *  A row that says only "ran Bash" makes the reader open every one of them to
 *  find the call they remember. The command, or the file, is what they are
 *  scanning for, so it goes where scanning finds it. */
const DETAIL_KEYS = ["command", "file_path", "path", "pattern", "query", "url", "notebook_path"];

export function toolDetail(args: unknown): string {
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const record = args as Record<string, unknown>;
  for (const key of DETAIL_KEYS) {
    const value = record[key];
    if (typeof value !== "string" || !value.trim()) continue;
    const line = value.trim().split("\n")[0];
    return line.length > 72 ? `${line.slice(0, 71)}…` : line;
  }
  return "";
}

/** How long the call took, once that is a fact.
 *
 *  Only a run this browser watched carries timing; a transcript loaded from the
 *  server does not, and the hook says so by returning nothing. An empty right
 *  edge is the honest answer there. */
function ToolElapsed() {
  const elapsed = useToolCallElapsed();
  if (elapsed === undefined) return null;
  const seconds = elapsed / 1000;
  return <span>{seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s</span>;
}

function ToolCall({
  toolName,
  args,
  argsText,
  result,
  running,
}: {
  toolName: string;
  args: unknown;
  argsText: string;
  result: unknown;
  running: boolean;
}) {
  const detail = toolDetail(args);
  return (
    <details className="tool" data-live={running || undefined}>
      <summary className="tool-head">
        <span className="tool-name">
          <span className="tool-caret" aria-hidden="true">
            ▸
          </span>
          <span className="tool-title">
            {running ? "running" : "ran"} {toolName}
          </span>
          {detail && <span className="tool-detail">{detail}</span>}
        </span>
        <span className="tool-meta">
          <ToolElapsed />
          {running && <span className="tool-live">LIVE</span>}
        </span>
      </summary>
      <pre>{argsText || JSON.stringify(args, null, 2)}</pre>
      {result !== undefined && <pre className="tool-result">{toolResultText(result)}</pre>}
    </details>
  );
}

function TextParts() {
  return (
    <MessagePrimitive.Parts>
      {({ part }) =>
        part.type === "text" ? (
          <MessagePartPrimitive.Text component="p" />
        ) : part.type === "tool-call" ? (
          <>
            <ToolCall
              toolName={part.toolName}
              args={part.args}
              argsText={part.argsText}
              result={part.result}
              running={part.status.type === "running"}
            />
            {/* Under the call it was asked about, because that is what it is
                about. A turn that ran three commands and stopped to ask about
                the second one reads that way, and whatever the agent said
                afterwards stays below the question it waited on. */}
            <CallApprovals toolCallId={part.toolCallId} />
          </>
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
export function errorText(error: unknown): string {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && "message" in error) {
    const { message } = error as { message: unknown };
    if (typeof message === "string") return message;
  }
  return "The agent run failed.";
}

export function AssistantMessage() {
  const error = useAuiState((state) => {
    const status = state.message.status;
    if (status?.type !== "incomplete" || status.reason !== "error") return undefined;
    return status.error;
  });

  return (
    <MessagePrimitive.Root className="message message-assistant">
      <TextParts />
      {/* Only what could not be placed beside a call: a request that named no
          call, or one whose call is not in the transcript. The end of the turn
          is where a request with nothing to sit beside belongs. */}
      <TurnApprovals />
      <MessagePrimitive.Error>
        <p className="notice message-error">
          <Ticked text={errorText(error)} />
        </p>
      </MessagePrimitive.Error>
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
    <>
      <div className="queue" aria-live="polite">
        <ComposerPrimitive.Queue>
          {() => (
            <div className="queued">
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
          className="btn"
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
        <button type="button" className="btn btn-primary" disabled={!canSend} onClick={send}>
          {isRunning ? "Queue" : "Send"}
        </button>
      </ComposerPrimitive.Root>
    </>
  );
}

type WorkspaceCustom = {
  workspaceRoot?: string;
  workspaceRef?: string;
  workspaceAttached?: boolean;
  workflowRunId?: string;
  workflowStepId?: string;
  editable?: boolean;
};

function WorkflowBacklink() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  if (!custom?.workflowRunId) return null;
  return (
    <a className="backlink" href={`/runs/${custom.workflowRunId}`}>
      ← Back to run {custom.workflowRunId}
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
function WorkspaceLine() {
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
    <div className="dock-workspace">
      {attached ? (
        <>
          <span className="micro">Working in</span>
          <code className="dock-path">cd {workspace.workspaceRoot}</code>
        </>
      ) : workspace.workspaceRef ? (
        <>
          <span className="micro">Detached — the work is on</span>
          <code className="dock-path">git checkout {workspace.workspaceRef}</code>
        </>
      ) : (
        <span className="micro">No worktree — attach one to give this chat somewhere to work</span>
      )}
      <button type="button" className="link-flame" onClick={() => void toggle()} disabled={busy}>
        {busy ? "Working…" : attached ? "Detach" : workspace.workspaceRef ? "Reattach" : "Attach"}
      </button>
      {error && (
        <p className="notice dock-error">
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
export function outcomeText(approval: ApiApproval): string {
  if (approval.status === "interrupted")
    return "Interrupted — the agent that asked this is gone, so it can no longer be answered.";
  if (approval.decisionSource === "session_grant")
    return "Approved automatically: you allowed this exact action for this conversation earlier.";
  if (approval.decisionSource === "policy")
    return approval.decision === "cancel"
      ? "Refused by the configured policy — the action did not run."
      : "Allowed by the configured policy, without asking.";
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

/** The one line a folded approval is worth: what happened, and to what. */
export function summaryText(approval: ApiApproval): string {
  const target =
    approval.command ?? approval.toolName ?? KIND_LABELS[approval.kind].toLowerCase();
  if (approval.status === "pending") return `Approval needed · ${target}`;
  if (approval.status === "interrupted") return `Interrupted · ${target}`;
  if (approval.decisionSource === "session_grant")
    return `Approved automatically · ${target}`;
  if (approval.decisionSource === "policy")
    return approval.decision === "cancel"
      ? `Refused by policy · ${target}`
      : `Allowed by policy · ${target}`;
  switch (approval.decision) {
    case "accept":
      return `Approved · ${target}`;
    case "accept_for_session":
      return `Approved for this conversation · ${target}`;
    case "cancel":
      return `Cancelled · ${target}`;
    default:
      return `Closed · ${target}`;
  }
}

/** What the turn is asking, and the answers this request permits.
 *
 *  Only those answers: a provider that never offered a session grant cannot
 *  honour one, so offering the button would be offering something we would
 *  have to refuse.
 *
 *  A `details` rather than a panel, because this outlives the question. While
 *  it is pending it is the only thing worth reading and is open; once it is
 *  answered it folds down to a line in the transcript beside the command it
 *  was about, where it is a record rather than a demand. */
export function ApprovalEntry({
  threadId,
  approval,
}: {
  threadId: string;
  approval: ApiApproval;
}) {
  const pending = approval.status === "pending";
  const [open, setOpen] = useState(pending);
  const [submitted, setSubmitted] = useState<ApprovalDecision>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    // Answered, so it stops being the thing under your eyes. Reopening is one
    // click, and a manual reopen survives because this only fires on the
    // transition.
    setOpen(pending);
  }, [pending]);

  async function decide(decision: ApprovalDecision) {
    setSubmitted(decision);
    setError(undefined);
    try {
      await decideApproval(threadId, approval.id, decision);
    } catch (failure) {
      // Stale, already answered, or a provider that has since died. The
      // decision did not land, so the controls come back with the reason.
      setSubmitted(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <details
      className={`approval approval-${pending ? "pending" : approval.status}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="approval-summary">{summaryText(approval)}</summary>
      <div className="approval-body" aria-live="polite">
        <header className="approval-head">
          <span className="approval-kind">{KIND_LABELS[approval.kind]}</span>
          <span className="approval-id">{approval.id}</span>
        </header>
        {approval.reason && <p className="approval-reason">{approval.reason}</p>}
        {approval.command && <pre className="approval-command">{approval.command}</pre>}
        {(approval.toolName || approval.cwd) && (
          <dl className="approval-facts">
            {approval.toolName && (
              <div>
                <dt>Tool</dt>
                <dd>{approval.toolName}</dd>
              </div>
            )}
            {approval.cwd && (
              <div>
                <dt>Working directory</dt>
                <dd>{approval.cwd}</dd>
              </div>
            )}
          </dl>
        )}
        <ApprovalArguments approval={approval} />
        {pending ? (
          <div className="approval-actions">
            {approval.allowedDecisions.map((decision) => (
              <button
                key={decision}
                type="button"
                className={`btn ${decision === "cancel" ? "" : "btn-primary"}`}
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
          <p className="approval-outcome">{outcomeText(approval)}</p>
        )}
        {error && (
          <p className="notice">
            <Ticked text={error} />
          </p>
        )}
      </div>
    </details>
  );
}

/** Every tool call the transcript holds, by id.
 *
 *  What decides where a request is shown: an approval naming a call in here has
 *  somewhere of its own to sit, and is therefore not part of any turn's
 *  leftovers. The whole thread rather than one message, because a restored
 *  transcript anchors its requests to the end while the calls they name are
 *  spread across the turns that made them.
 *
 *  A fact about the thread, so it is derived once above the turns rather than
 *  by each of them. The thread store hands out a new `messages` array on every
 *  streamed chunk, and a turn that read it would rescan every part in the
 *  conversation each time a token landed -- once per turn on screen, over
 *  transcripts that routinely carry hundreds of calls. */
const ToolCallIds = createContext<ReadonlySet<string>>(new Set<string>());

export function useToolCallIds(): ReadonlySet<string> {
  return useContext(ToolCallIds);
}

/** Derive it, and keep the same set while the ids in it are the same.
 *
 *  Identity is the whole point: a new set on every chunk would re-render every
 *  turn that reads one, which is the cost this exists to avoid. Tokens arrive
 *  far more often than tool calls do, so most rescans find nothing new. */
export function ToolCallIndex({ children }: { children: ReactNode }) {
  const messages = useAuiState((state) => state.thread.messages);
  const held = useRef<ReadonlySet<string>>(new Set<string>());
  const ids = useMemo(() => {
    const found = new Set<string>();
    for (const message of messages) {
      if (!Array.isArray(message.content)) continue;
      for (const part of message.content) {
        if (part.type === "tool-call") found.add(part.toolCallId);
      }
    }
    const previous = held.current;
    if (previous.size === found.size && [...found].every((id) => previous.has(id))) {
      return previous;
    }
    held.current = found;
    return found;
  }, [messages]);

  return <ToolCallIds.Provider value={ids}>{children}</ToolCallIds.Provider>;
}

function ApprovalList({
  threadId,
  entries,
  className,
}: {
  threadId: string;
  entries: readonly InlineApproval[];
  className: string;
}) {
  if (!entries.length) return null;
  return (
    <div className={className}>
      {entries.map(({ approval }) => (
        <ApprovalEntry key={approval.id} threadId={threadId} approval={approval} />
      ))}
    </div>
  );
}

/** What this conversation was asked to allow before one particular call.
 *
 *  Matched by the call's id rather than by where the request happened to arrive
 *  on the stream, so the pairing is the one the provider stated and survives a
 *  reload, a reconnect, and a turn that asked about only some of its calls. */
function CallApprovals({ toolCallId }: { toolCallId: string }) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const approvals = useApprovals(remoteId);
  const mine = useMemo(
    () => approvals.filter((entry) => entry.approval.toolCallId === toolCallId),
    [approvals, toolCallId],
  );

  if (!remoteId) return null;
  return (
    <ApprovalList
      threadId={remoteId}
      entries={mine}
      className="approvals approvals-call"
    />
  );
}

/** What this assistant turn stopped to ask about and nothing else can hold.
 *
 *  A request that named no call, or that named one this transcript does not
 *  contain -- a provider that paused over something other than a tool call, or
 *  a record written before the pairing existed. Anchored by index rather than
 *  pinned to the newest turn, so it stays with the turn that raised it once the
 *  conversation has moved on. An anchor past the end of the transcript belongs
 *  to the reply still being written, which is the only turn that can be
 *  paused. */
function TurnApprovals() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const index = useAuiState((state) => state.message.index);
  const isLast = useAuiState((state) => state.message.isLast);
  const total = useAuiState((state) => state.thread.messages.length);
  const approvals = useApprovals(remoteId);
  const placed = useToolCallIds();
  const mine = useMemo(
    () =>
      approvals.filter(
        (entry) =>
          (entry.messageIndex === index || (isLast && entry.messageIndex >= total)) &&
          !(entry.approval.toolCallId && placed.has(entry.approval.toolCallId)),
      ),
    [approvals, index, isLast, placed, total],
  );

  if (!remoteId) return null;
  return <ApprovalList threadId={remoteId} entries={mine} className="approvals" />;
}

/** Requests for a reply assistant-ui has not mounted yet.
 *
 *  Workflow runs can begin outside this browser. Their approval feed must be
 *  visible immediately, before transcript streaming creates the assistant
 *  message that will ultimately own the card. Once that message appears the
 *  normal turn placement takes over and this slot empties itself. */
export function UnanchoredApprovals() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const total = useAuiState((state) => state.thread.messages.length);
  const approvals = useApprovals(remoteId);
  const placed = useToolCallIds();
  const unanchored = useMemo(
    () =>
      approvals.filter(
        (entry) =>
          entry.messageIndex >= total &&
          !(entry.approval.toolCallId && placed.has(entry.approval.toolCallId)),
      ),
    [approvals, placed, total],
  );

  if (!remoteId) return null;
  return (
    <ApprovalList
      threadId={remoteId}
      entries={unanchored}
      className="approvals approvals-live"
    />
  );
}

/** The line of figures under the conversation heading.
 *
 *  Every cell is counted from the transcript this browser is holding, so each
 *  one is a fact about what is on screen rather than an estimate of anything. */
export function ConversationStats() {
  const messages = useAuiState((state) => state.thread.messages);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const approvals = useApprovals(remoteId);

  const toolCalls = useMemo(
    () =>
      messages.reduce(
        (total, message) =>
          total +
          (Array.isArray(message.content)
            ? message.content.filter((part) => part.type === "tool-call").length
            : 0),
        0,
      ),
    [messages],
  );
  const pending = approvals.filter((entry) => entry.approval.status === "pending").length;

  return (
    <StatStrip>
      <Stat label="Turns" value={messages.length} />
      <Stat label="Tool calls" value={toolCalls} />
      <Stat
        label="Approvals"
        value={pending ? `${approvals.length} · ${pending} open` : approvals.length}
        tone={pending ? "alert" : undefined}
      />
      <Stat
        label="Status"
        value={isRunning ? "Live" : "Idle"}
        tone={isRunning ? "live" : undefined}
      />
    </StatStrip>
  );
}

export function ChatThread() {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="stream">
        <WorkflowBacklink />
        <ThreadPrimitive.Empty>
          <div className="welcome">
            <div className="welcome-copy">
              <p className="eyebrow">OpenEngine / Chat</p>
              <h1>Start a conversation.</h1>
              <p className="lede">Each chat has its own agent history and Git worktree.</p>
            </div>
          </div>
        </ThreadPrimitive.Empty>
        {/* Renders no element of its own, so the viewport's children are still
            the messages. Held above them so the rescan happens once per chunk
            rather than once per turn per chunk. */}
        <ToolCallIndex>
          <ThreadPrimitive.Messages>
            {({ message }) =>
              message.role === "user" ? <UserMessage /> : <AssistantMessage />
            }
          </ThreadPrimitive.Messages>
          <UnanchoredApprovals />
        </ToolCallIndex>
        <Dock />
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

function Dock() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | WorkspaceCustom
    | undefined;
  const workflowConversation = Boolean(custom?.workflowRunId);
  const editable = !workflowConversation || Boolean(custom?.editable);
  return (
    <ThreadPrimitive.ViewportFooter className="dock">
      <ThreadPrimitive.ScrollToBottom className="btn jump-button">
        Jump to latest
      </ThreadPrimitive.ScrollToBottom>
      {!editable ? (
        <p className="step-note">
          This transcript belongs to a workflow step. Return to the run for status and actions.
        </p>
      ) : (
        <>
          <Composer />
          {!workflowConversation && (
            <div className="dock-foot">
              {/* Under the composer rather than in the heading: a detached chat
                  refuses to run, so the way to fix that cannot be somewhere you
                  have to scroll a long conversation to reach. */}
              <WorkspaceLine />
            </div>
          )}
        </>
      )}
    </ThreadPrimitive.ViewportFooter>
  );
}
