import {
  AssistantRuntimeProvider,
  RuntimeAdapterProvider,
  fromThreadMessageLike,
  useAui,
  useLocalRuntime,
  useRemoteThreadListRuntime,
  type ChatModelAdapter,
  type RemoteThreadListAdapter,
  type ThreadHistoryAdapter,
  type ThreadAssistantMessagePart,
  type ThreadMessage,
} from "@assistant-ui/react";
import { createAssistantStream } from "assistant-stream";
import {
  createContext,
  useEffect,
  useContext,
  useMemo,
  useRef,
  useState,
  type PropsWithChildren,
} from "react";

import {
  api,
  messageText,
  runNotStartedError,
  type ApiApproval,
  type ApiHistory,
  type ApiThread,
} from "./api";
import { publishApproval } from "./approvals";

type NewChatDefaults = {
  agentId: string;
  runner: string;
};

type ThreadInitializer = {
  current: (() => Promise<{ remoteId: string }>) | null;
};

const DefaultsContext = createContext<NewChatDefaults | null>(null);
const ACTIVE_THREAD_KEY = "engine.activeThreadId";

function useInitialThreadId(forcedThreadId?: string) {
  const storedThreadId = useRef(
    forcedThreadId ?? (typeof window === "undefined"
      ? undefined
      : window.localStorage.getItem(ACTIVE_THREAD_KEY) ?? undefined),
  ).current;
  const [result, setResult] = useState<{ loading: boolean; threadId?: string }>(() => ({
    loading: storedThreadId !== undefined,
  }));

  useEffect(() => {
    if (!storedThreadId) return;
    let cancelled = false;
    api<ApiThread>(`/api/threads/${storedThreadId}`)
      .then((thread) => {
        if (cancelled) return;
        if (thread.archived) window.localStorage.removeItem(ACTIVE_THREAD_KEY);
        setResult({ loading: false, threadId: thread.archived ? undefined : thread.id });
      })
      .catch(() => {
        if (cancelled) return;
        window.localStorage.removeItem(ACTIVE_THREAD_KEY);
        setResult({ loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [storedThreadId]);

  return result;
}

function ThreadInitializationBridge({
  initializer,
  reloadThreads,
}: {
  initializer: ThreadInitializer;
  reloadThreads: { current: (() => Promise<void>) | null };
}) {
  const aui = useAui();
  initializer.current = () => aui.threadListItem.initialize();
  reloadThreads.current = () => aui.threads.reload();
  return null;
}

function remoteMetadata(thread: ApiThread) {
  return {
    remoteId: thread.id,
    status: thread.archived ? ("archived" as const) : ("regular" as const),
    title: thread.title,
    custom: {
      agentId: thread.agentId,
      runner: thread.runner,
      workspaceRoot: thread.workspaceRoot,
      workspaceRef: thread.workspaceRef,
      workspaceAttached: thread.workspaceAttached,
      workflowRunId: thread.workflowRunId,
      workflowStepId: thread.workflowStepId,
      editable: thread.editable,
    },
  };
}

/** `messageIndex` is where the assistant turn this stream is producing will sit
 *  in the thread, which is the only moment anybody knows it: an approval
 *  belongs beside the command it is about, and by the time it is answered the
 *  turn it interrupted may no longer be the newest one. */
export async function* readRunResponse(
  response: Response,
  threadId: string,
  messageIndex: number,
) {
  if (response.status === 204) return;
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line) continue;
      const event = JSON.parse(line) as
        | { type: "content" | "done"; content: ThreadAssistantMessagePart[] }
        | { type: "approval"; approval: ApiApproval | null }
        | { type: "error"; error: string };
      if (event.type === "approval") {
        // Snapshots ride the same stream as message content and carry none of
        // it. A reconnecting browser is sent the current one from scratch, so
        // publishing whatever arrives is what restores a pause after a refresh.
        publishApproval(threadId, event.approval, messageIndex);
        continue;
      }
      if (event.type === "error") throw new Error(event.error);
      yield { content: event.content };
    }
    if (done) break;
  }
}

function HistoryProvider({ children }: PropsWithChildren) {
  const aui = useAui();
  const history = useMemo<ThreadHistoryAdapter>(
    () => ({
      async load() {
        const { remoteId } = aui.threadListItem.getState();
        if (!remoteId) return { messages: [] };
        const rows = await api<ApiHistory>(
          `/api/threads/${remoteId}/messages`,
        );
        // Restoring the transcript restores what it was asked to allow. Only a
        // run this process is still executing has a stream to replay these on,
        // so a step that has finished -- or a browser that arrives after one
        // did -- would otherwise show a conversation that had never paused.
        //
        // All on the turn the transcript ends on, because nothing durable ties
        // a request to the turn that raised it: the anchor is observed while a
        // reply streams, and a reload has no stream to observe. That end is the
        // reply being resumed when the transcript stops at a user message and
        // the last reply otherwise, which is the anchor `resume` would publish
        // under, so the two paths land one card rather than two.
        const anchor =
          rows.messages.length -
          (rows.messages.at(-1)?.role === "assistant" ? 1 : 0);
        for (const approval of rows.approvals)
          publishApproval(remoteId, approval, anchor);
        let parentId: string | null = null;
        return {
          unstable_resume: rows.unstable_resume,
          messages: rows.messages.map((row) => {
            const message = fromThreadMessageLike(
              { id: row.id, role: row.role, content: row.content },
              row.id,
              { type: "complete", reason: "stop" },
            );
            const item = { parentId, message };
            parentId = row.id;
            return item;
          }),
        };
      },
      async *resume({ abortSignal }) {
        const { remoteId } = aui.threadListItem.getState();
        if (!remoteId) return;
        // A resumed run is one whose user message is stored and whose reply is
        // not, so the turn being rejoined lands after everything loaded.
        const messageIndex = aui.thread.getState().messages.length;
        const response = await fetch(`/api/threads/${remoteId}/runs/current`, {
          signal: abortSignal,
        });
        yield* readRunResponse(response, remoteId, messageIndex);
      },
      // AgentSession.say persists both sides of a turn atomically. The history
      // adapter is load-only so assistant-ui does not duplicate those writes.
      async append() {},
    }),
    [aui],
  );
  return <RuntimeAdapterProvider adapters={{ history }}>{children}</RuntimeAdapterProvider>;
}

export function EngineRuntimeProvider({
  defaults,
  children,
  initialThreadId,
}: PropsWithChildren<{ defaults: NewChatDefaults; initialThreadId?: string }>) {
  const initialThread = useInitialThreadId(initialThreadId);
  if (initialThread.loading)
    return <main className="loading">Restoring chats…</main>;

  return (
    <EngineRuntime
      defaults={defaults}
      initialThreadId={initialThread.threadId}
    >
      {children}
    </EngineRuntime>
  );
}

function EngineRuntime({
  defaults,
  initialThreadId,
  children,
}: PropsWithChildren<{ defaults: NewChatDefaults; initialThreadId?: string }>) {
  const defaultsRef = useRef(defaults);
  const threadInitializerRef = useRef<ThreadInitializer["current"]>(null);
  const reloadThreadsRef = useRef<(() => Promise<void>) | null>(null);
  defaultsRef.current = defaults;

  const modelAdapter = useMemo<ChatModelAdapter>(
    () => ({
      async *run({ messages, abortSignal, unstable_threadId }) {
        const lastUser = [...messages].reverse().find((message) => message.role === "user");
        const text = lastUser ? messageText(lastUser) : "";
        if (!text) throw new Error("Cannot send an empty message.");

        let response: Response;
        let started = "";
        try {
          const threadId =
            unstable_threadId ?? (await threadInitializerRef.current?.())?.remoteId;
          if (!threadId) throw new Error("The chat could not be initialized.");
          started = threadId;

          // assistant-ui normally generates this after runEnd; doing it here
          // makes the title the first model turn for a new conversation.
          //
          // Neither call names a runner: the conversation keeps the last one it
          // was given, and sending the page's new-chat default with every turn
          // would silently move an older chat onto it.
          // Naming is cosmetic and sending is not, so a chat that cannot be
          // named is still a chat to send to. Anything but an abort is
          // swallowed: the alternative is reporting "the run could not be
          // started" for a run nothing has tried to start yet.
          await api(`/api/threads/${threadId}/title`, {
            method: "POST",
            body: JSON.stringify({ text }),
            signal: abortSignal,
          }).catch((failure: unknown) => {
            if (failure instanceof Error && failure.name === "AbortError") throw failure;
          });
          await reloadThreadsRef.current?.();

          response = await fetch(`/api/threads/${threadId}/runs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
            signal: abortSignal,
          });
          if (!response.ok || !response.body) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.error ?? `${response.status} ${response.statusText}`);
          }
        } catch (error) {
          if (error instanceof Error && error.name === "AbortError") throw error;
          throw runNotStartedError(error);
        }
        // `messages` ends with the message that started this turn, so the reply
        // being streamed is the one after it.
        yield* readRunResponse(response, started, messages.length);
      },
    }),
    [],
  );

  const threadAdapter = useMemo<RemoteThreadListAdapter>(
    () => ({
      async list() {
        const result = await api<{ threads: ApiThread[] }>("/api/threads");
        return { threads: result.threads.map(remoteMetadata) };
      },
      async initialize() {
        const thread = await api<ApiThread>("/api/threads", {
          method: "POST",
          body: JSON.stringify(defaultsRef.current),
        });
        return { remoteId: thread.id };
      },
      async rename(remoteId, title) {
        await api(`/api/threads/${remoteId}`, {
          method: "PATCH",
          body: JSON.stringify({ title }),
        });
      },
      async archive(remoteId) {
        await api(`/api/threads/${remoteId}/archive`, { method: "POST" });
      },
      async unarchive(remoteId) {
        await api(`/api/threads/${remoteId}/unarchive`, { method: "POST" });
      },
      async delete(remoteId) {
        await api(`/api/threads/${remoteId}`, { method: "DELETE" });
      },
      async fetch(remoteId) {
        return remoteMetadata(await api<ApiThread>(`/api/threads/${remoteId}`));
      },
      async generateTitle(remoteId, messages: readonly ThreadMessage[]) {
        return createAssistantStream(async (controller) => {
          const result = await api<{ title: string }>(`/api/threads/${remoteId}/title`, {
            method: "POST",
            body: JSON.stringify({ messages }),
          });
          controller.appendText(result.title);
        });
      },
      unstable_Provider: HistoryProvider,
    }),
    [],
  );

  const runtime = useRemoteThreadListRuntime({
    runtimeHook: () =>
      useLocalRuntime(modelAdapter, {
        unstable_enableMessageQueue: true,
        // Preserve queued follow-ups while Stop transitions to the next run.
        unstable_queueClearOnCancel: false,
      }),
    adapter: threadAdapter,
    initialThreadId,
    onThreadIdChange(threadId) {
      if (threadId) window.localStorage.setItem(ACTIVE_THREAD_KEY, threadId);
      else window.localStorage.removeItem(ACTIVE_THREAD_KEY);
    },
  });

  return (
    <DefaultsContext.Provider value={defaults}>
      <AssistantRuntimeProvider runtime={runtime}>
        <ThreadInitializationBridge
          initializer={threadInitializerRef}
          reloadThreads={reloadThreadsRef}
        />
        {children}
      </AssistantRuntimeProvider>
    </DefaultsContext.Provider>
  );
}

export function useNewChatDefaults() {
  const defaults = useContext(DefaultsContext);
  if (!defaults) throw new Error("useNewChatDefaults must be inside EngineRuntimeProvider");
  return defaults;
}
