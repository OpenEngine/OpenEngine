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

import { api, messageText, type ApiHistory, type ApiThread } from "./api";

type NewChatDefaults = {
  agentId: string;
  runner: string;
};

type ThreadInitializer = {
  current: (() => Promise<{ remoteId: string }>) | null;
};

const DefaultsContext = createContext<NewChatDefaults | null>(null);
const ACTIVE_THREAD_KEY = "engine.activeThreadId";

function useInitialThreadId() {
  const storedThreadId = useRef(
    typeof window === "undefined"
      ? undefined
      : window.localStorage.getItem(ACTIVE_THREAD_KEY) ?? undefined,
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
    },
  };
}

async function* readRunResponse(response: Response) {
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
        | { type: "error"; error: string };
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
        const response = await fetch(`/api/threads/${remoteId}/runs/current`, {
          signal: abortSignal,
        });
        yield* readRunResponse(response);
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
}: PropsWithChildren<{ defaults: NewChatDefaults }>) {
  const initialThread = useInitialThreadId();
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
        const threadId =
          unstable_threadId ?? (await threadInitializerRef.current?.())?.remoteId;
        if (!threadId) throw new Error("The chat could not be initialized.");
        const lastUser = [...messages].reverse().find((message) => message.role === "user");
        const text = lastUser ? messageText(lastUser) : "";
        if (!text) throw new Error("Cannot send an empty message.");

        // assistant-ui normally generates this after runEnd; doing it here
        // makes the title the first model turn for a new conversation.
        await api(`/api/threads/${threadId}/title`, {
          method: "POST",
          body: JSON.stringify({ text, runner: defaultsRef.current.runner }),
          signal: abortSignal,
        });
        await reloadThreadsRef.current?.();

        const response = await fetch(`/api/threads/${threadId}/runs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, runner: defaultsRef.current.runner }),
          signal: abortSignal,
        });
        yield* readRunResponse(response);
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
        // Stop means stop: discard follow-ups instead of starting one after
        // the cancelled run settles.
        unstable_queueClearOnCancel: true,
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
