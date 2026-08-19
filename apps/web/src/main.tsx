import { useAui, useAuiState } from "@assistant-ui/react";
import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  api,
  setThreadRunner,
  type ApiThread,
  type EngineConfig,
  type RunnerOption,
} from "./api";
import { ChatThread, ConversationStats } from "./chat";
import { EngineRuntimeProvider } from "./runtime";
import { NewWorkflowPage, RunDetailPage, RunsPage, useRuns } from "./runs";
import { Sidebar, type RailSection } from "./sidebar";
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
  workflowRunId?: string;
  editable?: boolean;
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
  const workflowConversation = Boolean(thread?.workflowRunId);
  const title = fetched?.title || listedTitle || "New chat";

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
        <p className="lede">
          {workflowConversation
            ? thread?.editable
              ? "A workflow step owns this transcript; sending guidance reactivates it if it has closed."
              : "A workflow step owns this transcript; its run chose the runner."
            : "This runner answers here until you pick another."}
        </p>
      </div>
      <div className="field">
        <span>Agent</span>
        <span className="field-box">{thread?.agentId ?? "…"}</span>
      </div>
      {workflowConversation ? (
        <div className="field">
          <span>Runner</span>
          <span className="field-box">{runner}</span>
        </div>
      ) : (
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
      )}
    </header>
  );
}

type Route =
  | { kind: "runs" }
  | { kind: "new-run" }
  | { kind: "run"; runId: string }
  | { kind: "chat"; threadId?: string; runId?: string };

function currentRoute(): Route {
  const path = window.location.pathname.replace(/\/$/, "") || "/";
  if (path === "/" || path === "/runs") return { kind: "runs" };
  if (path === "/runs/new") return { kind: "new-run" };
  const workflowConversation = path.match(
    /^\/runs\/([^/]+)\/conversations\/([^/]+)$/,
  );
  if (workflowConversation)
    return {
      kind: "chat",
      runId: decodeURIComponent(workflowConversation[1]),
      threadId: decodeURIComponent(workflowConversation[2]),
    };
  if (path.startsWith("/runs/"))
    return { kind: "run", runId: decodeURIComponent(path.slice("/runs/".length)) };
  if (path.startsWith("/conversations/"))
    return { kind: "chat", threadId: decodeURIComponent(path.slice("/conversations/".length)) };
  return { kind: "chat" };
}

/** Which section of the rail the page on screen came from, so the rail opens
 *  showing where you are. A workflow's own conversation belongs to its run. */
function sectionFor(route: Route): RailSection {
  if (route.kind === "chat") return route.runId ? "workflows" : "chats";
  return "workflows";
}

/** One shell for every screen: the rail, and the page beside it.
 *
 *  The rail lists chats wherever it is drawn, so the chat runtime is mounted
 *  around the workflow pages too -- reading the list is all they ask of it. */
function App() {
  const route = useMemo(currentRoute, []);
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");
  const { runs, error: runsError } = useRuns();

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        setAgentId(value.defaultAgent);
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  if (error)
    return <main className="state state-fatal">Could not connect to openengine: {error}</main>;
  if (!config || !agentId || !runner)
    return <main className="state">Starting openengine…</main>;

  const chat = route.kind === "chat";
  // Switching the open conversation in place is for the rail beside a chat of
  // its own. A workflow step's transcript is reached through its run, so
  // leaving one is a move to another page rather than a swap under the URL.
  const standaloneChat = chat && !route.runId;
  const activeRunId = route.kind === "run" || route.kind === "chat" ? route.runId : undefined;
  return (
    <EngineRuntimeProvider
      defaults={{ agentId, runner }}
      initialThreadId={chat ? route.threadId : undefined}
      rememberActiveThread={chat}
    >
      <div className="app-shell">
        <Sidebar
          runs={runs}
          initialSection={sectionFor(route)}
          linkChats={!standaloneChat}
          activeRunId={activeRunId}
          activeConversationUrl={
            chat && route.runId ? window.location.pathname.replace(/\/$/, "") : undefined
          }
          activeView={route.kind === "runs" ? "runs" : route.kind === "new-run" ? "new" : undefined}
        />
        {route.kind === "runs" ? (
          <RunsPage runs={runs} error={runsError} />
        ) : route.kind === "new-run" ? (
          <NewWorkflowPage config={config} />
        ) : route.kind === "run" ? (
          <RunDetailPage runId={route.runId} />
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
    <App />
  </StrictMode>,
);
