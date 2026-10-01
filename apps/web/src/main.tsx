import { useAui, useAuiState } from "@assistant-ui/react";
import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  api,
  newChatAgent,
  setThreadRunner,
  type ApiThread,
  type EngineConfig,
  type RunnerOption,
} from "./api";
import { AuthGate } from "./auth";
import { ChatThread, ConversationStats } from "./chat";
import { GraphConversationPage } from "./graph-conversation";
import { EngineRuntimeProvider } from "./runtime";
import {
  NewWorkflowPage,
  RunDetailPage,
  RunsPage,
  useGraphNodes,
  useRuns,
} from "./runs";
import { routeForPath, type Route } from "./routes";
import { Sidebar } from "./sidebar";
import { UtilizationPage } from "./utilization";
import "./styles.css";

function ChatPanel({
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
  return (
    <main className="panel">
      <ChatHeader
        config={config}
        agentId={agentId}
        runner={runner}
        onAgentChange={onAgentChange}
        onRunnerChange={onRunnerChange}
      />
      <ConversationStats />
      <ChatThread />
    </main>
  );
}

type ThreadCustom = {
  agentId?: string;
  runner?: string;
};

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
    <header className="panel-head">
      <div className="panel-head-copy">
        <p className="eyebrow">New chat defaults</p>
        <h1>New conversation</h1>
        <p className="lede">Choose what starts the next conversation and which runner answers.</p>
      </div>
      <label className="field">
        <span>Agent</span>
        <select
          className="field-box"
          value={agentId}
          onChange={(event) => onAgentChange(event.target.value)}
        >
          {config.agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.id} — {agent.description}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Runner</span>
        <select
          className="field-box"
          value={runner}
          onChange={(event) => onRunnerChange(event.target.value)}
        >
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
  const listedTitle = useAuiState((state) => state.threadListItem.title);
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
  // Title generation refreshes the thread list after the first message. That
  // refreshed value can be newer than the conversation snapshot fetched when
  // this header first mounted.
  const title = listedTitle || fetched?.title || "New chat";

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
    <header className="panel-head">
      <div className="panel-head-copy">
        <p className="eyebrow">This conversation</p>
        <h1>{title}</h1>
      </div>
      <div className="field">
        <span>Agent</span>
        <span className="field-box">{thread?.agentId ?? "…"}</span>
      </div>
      <div className="panel-head-controls">
        <label className="field">
          <span>Runner</span>
          <select
            className="field-box"
            value={runner}
            onChange={(event) => void choose(event.target.value)}
          >
            {runners.map((option) => (
              <option key={option.id} value={option.id}>
                {option.id}
              </option>
            ))}
          </select>
          {error && <span className="field-error">{error}</span>}
        </label>
      </div>
    </header>
  );
}

function currentRoute(): Route {
  return routeForPath(window.location.pathname);
}

/** One shell for every screen: the rail, and the page beside it.
 *
 *  The chat runtime is mounted around every page so conversation routes can
 *  render their thread while workflow pages keep their own run UI. */
function App() {
  const route = useMemo(currentRoute, []);
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");
  const { runs, error: runsError, remove: deleteRun } = useRuns();
  // What a graph WorkOrder offers in the rail: its graph's nodes, since a
  // graph run has no steps for the list to carry.
  const graphNodes = useGraphNodes(runs);
  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        setAgentId(newChatAgent(value));
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  if (error)
    return <main className="state state-fatal">Could not connect to openengine: {error}</main>;
  if (!config || !agentId || !runner)
    return <main className="state">Starting openengine…</main>;

  const chat = route.kind === "chat";
  const activeRunId = route.kind === "run" || route.kind === "chat" || route.kind === "graph-conversation" ? route.runId : undefined;
  const conversationUrl =
    chat || route.kind === "graph-conversation"
      ? window.location.pathname.replace(/\/$/, "")
      : undefined;
  const sidebar = () => (
    <Sidebar
      runs={runs}
      graphNodes={graphNodes}
      runners={config.runners.map((option) => option.id)}
      activeRunId={activeRunId}
      activeConversationUrl={conversationUrl}
      activeView={
        route.kind === "runs"
          ? "runs"
          : route.kind === "new-run"
            ? "new"
            : route.kind === "utilization"
              ? "utilization"
              : undefined
      }
      onDeleteRun={deleteRun}
    />
  );
  return (
    <EngineRuntimeProvider
      defaults={{ agentId, runner }}
      initialThreadId={chat ? route.threadId : undefined}
      rememberActiveThread={chat}
      deferMount={chat}
      fallback={
        <div className="app-shell">
          {sidebar()}
          <main className="loading">Restoring chat…</main>
        </div>
      }
    >
      <div className="app-shell">
        {sidebar()}
        {route.kind === "runs" ? (
          <RunsPage runs={runs.filter((run) => run.phase !== "scheduled")} error={runsError} />
        ) : route.kind === "new-run" ? (
          <NewWorkflowPage config={config} />
        ) : route.kind === "run" ? (
          <RunDetailPage runId={route.runId} />
        ) : route.kind === "graph-conversation" ? (
          <GraphConversationPage
            runId={route.runId}
            nodeId={route.nodeId}
            workOrderName={runs.find((run) => run.runId === route.runId)?.name}
          />
        ) : route.kind === "utilization" ? (
          <UtilizationPage />
        ) : (
          <ChatPanel
            config={config}
            agentId={agentId}
            runner={runner}
            onAgentChange={setAgentId}
            onRunnerChange={setRunner}
          />
        )}
      </div>
    </EngineRuntimeProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </StrictMode>,
);
