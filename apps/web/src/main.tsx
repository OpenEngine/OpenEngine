import { useAui, useAuiState } from "@assistant-ui/react";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  api,
  setThreadRunner,
  type ApiThread,
  type EngineConfig,
  type RunnerOption,
} from "./api";
import { ChatSidebar, ChatThread } from "./chat";
import { EngineRuntimeProvider } from "./runtime";
import { NewWorkflowPage, RunDetailPage, RunsPage } from "./runs";
import "./styles.css";

function ChatApp({ initialThreadId }: { initialThreadId?: string }) {
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        setAgentId(value.defaultAgent);
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  if (error) return <main className="fatal">Could not connect to openengine: {error}</main>;
  if (!config || !agentId || !runner)
    return <main className="loading">Starting openengine…</main>;

  return (
    <EngineRuntimeProvider defaults={{ agentId, runner }} initialThreadId={initialThreadId}>
      <div className="app-shell">
        <ChatSidebar />
        <main className="chat-panel">
          <ChatHeader
            config={config}
            agentId={agentId}
            runner={runner}
            onAgentChange={setAgentId}
            onRunnerChange={setRunner}
          />
          <ChatThread />
        </main>
      </div>
    </EngineRuntimeProvider>
  );
}

type ThreadCustom = { agentId?: string; runner?: string; workflowRunId?: string };

/** The header speaks for whatever is on screen: the defaults the next
 *  conversation starts from, or the open conversation and who answers it. */
function ChatHeader({
  config,
  agentId,
  runner,
  onAgentChange,
  onRunnerChange,
}: {
  config: EngineConfig;
  agentId: string;
  runner: string;
  onAgentChange: (agentId: string) => void;
  onRunnerChange: (runner: string) => void;
}) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | ThreadCustom
    | undefined;

  if (remoteId)
    return (
      // Keyed by conversation: switching chats must not leave the previous
      // one's in-flight choice on screen.
      <ConversationHeader
        key={remoteId}
        threadId={remoteId}
        listed={custom}
        runners={config.runners}
        fallbackRunner={runner}
      />
    );

  return (
    <header className="chat-header">
      <div className="header-copy">
        <span className="eyebrow">NEW CHAT DEFAULTS</span>
        <p>Choose what starts the next conversation and which runner answers.</p>
      </div>
      <label>
        <span>Agent</span>
        <select value={agentId} onChange={(event) => onAgentChange(event.target.value)}>
          {config.agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.id} — {agent.description}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Runner</span>
        <select value={runner} onChange={(event) => onRunnerChange(event.target.value)}>
          {config.runners.map((option) => (
            <option key={option.id} value={option.id}>
              {option.id}
            </option>
          ))}
        </select>
      </label>
    </header>
  );
}

/** The open conversation's own runner, which the server remembers between
 *  turns. Its agent was settled when the chat was created, so that one is
 *  shown as the fact it is. */
function ConversationHeader({
  threadId,
  listed,
  runners,
  fallbackRunner,
}: {
  threadId: string;
  listed?: ThreadCustom;
  runners: RunnerOption[];
  fallbackRunner: string;
}) {
  const aui = useAui();
  // Read the conversation rather than trusting the cached thread list for
  // this: the dropdown claims to name the runner that answers here, and the
  // list is a snapshot taken whenever it was last refreshed.
  const [fetched, setFetched] = useState<ApiThread>();
  const [chosen, setChosen] = useState<string>();
  const [error, setError] = useState<string>();
  const thread = fetched ?? listed;
  // A chat nothing has described yet was started on the defaults, so those are
  // the truthful thing to show while it is being read.
  const runner = chosen ?? thread?.runner ?? fallbackRunner;
  const workflowConversation = Boolean(thread?.workflowRunId);

  useEffect(() => {
    let current = true;
    void api<ApiThread>(`/api/threads/${threadId}`)
      .then((value) => {
        if (current) setFetched(value);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [threadId]);

  async function choose(next: string) {
    setChosen(next);
    setError(undefined);
    try {
      setFetched(await setThreadRunner(threadId, next));
      // The sidebar prints the same runner under every chat's title.
      await aui.threads.reload();
    } catch (failure) {
      setChosen(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <header className="chat-header">
      <div className="header-copy">
        <span className="eyebrow">THIS CONVERSATION</span>
        <p>
          {workflowConversation
            ? "A workflow step owns this transcript; its run chose the runner."
            : "This runner answers here until you pick another."}
        </p>
      </div>
      <div className="header-fact">
        <span>Agent</span>
        <span className="header-value">{thread?.agentId ?? "…"}</span>
      </div>
      {workflowConversation ? (
        <div className="header-fact">
          <span>Runner</span>
          <span className="header-value">{runner}</span>
        </div>
      ) : (
        <label>
          <span>Runner</span>
          <select value={runner} onChange={(event) => void choose(event.target.value)}>
            {runners.map((option) => (
              <option key={option.id} value={option.id}>
                {option.id}
              </option>
            ))}
          </select>
          {error && <span className="header-error">{error}</span>}
        </label>
      )}
    </header>
  );
}

function App() {
  const path = window.location.pathname.replace(/\/$/, "") || "/";
  if (path === "/" || path === "/runs") return <RunsPage />;
  if (path === "/runs/new") return <NewWorkflowPage />;
  if (path.startsWith("/runs/")) {
    return <RunDetailPage runId={decodeURIComponent(path.slice("/runs/".length))} />;
  }
  if (path.startsWith("/conversations/")) {
    return <ChatApp initialThreadId={decodeURIComponent(path.slice("/conversations/".length))} />;
  }
  return <ChatApp />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
