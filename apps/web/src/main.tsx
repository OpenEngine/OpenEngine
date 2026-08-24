import { useAui, useAuiState } from "@assistant-ui/react";
import { StrictMode, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import {
  api,
  newChatAgent,
  setThreadAutoApprove,
  setThreadRunner,
  type ApiThread,
  type ApiProject,
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
  autoApprove?: boolean;
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
  const [chosenAutoApprove, setChosenAutoApprove] = useState<boolean>();
  const [autoApproveBusy, setAutoApproveBusy] = useState(false);
  const [error, setError] = useState<string>();
  const thread = fetched ?? listed;
  // A chat nothing has described yet was started on the defaults, so those are
  // the truthful thing to show while it is being read.
  const runner = chosen ?? thread?.runner ?? fallbackRunner;
  const workflowConversation = Boolean(thread?.workflowRunId);
  const implementationConversation = workflowConversation && Boolean(thread?.editable);
  const autoApprove = chosenAutoApprove ?? thread?.autoApprove ?? false;
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

  async function chooseAutoApprove(next: boolean) {
    setChosenAutoApprove(next);
    setAutoApproveBusy(true);
    setError(undefined);
    try {
      setFetched(await setThreadAutoApprove(threadId, next));
    } catch (failure) {
      setChosenAutoApprove(undefined);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setAutoApproveBusy(false);
    }
  }

  return (
    <header
      className={`panel-head ${implementationConversation ? "panel-head-implementation" : ""}`}
    >
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
      {workflowConversation && (
        <label className="field">
          <span>Approvals</span>
          <span className="field-box auto-approve-control">
            <input
              type="checkbox"
              checked={autoApprove}
              disabled={autoApproveBusy}
              onChange={(event) => void chooseAutoApprove(event.target.checked)}
            />
            <span>{autoApproveBusy ? "Saving…" : "Auto-approve"}</span>
          </span>
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
  /** `plan` is the same chat page, opened on the agent that plans rather than
   *  on the one that codes -- and always on a new conversation, because a
   *  button that offered you the last one back would not be a plan. */
  | { kind: "chat"; threadId?: string; runId?: string; plan?: boolean };

function currentRoute(): Route {
  const path = window.location.pathname.replace(/\/$/, "") || "/";
  if (path === "/" || path === "/runs") return { kind: "runs" };
  if (path === "/runs/new") return { kind: "new-run" };
  if (path === "/plan") return { kind: "chat", plan: true };
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
 *  showing where you are. A workflow's own conversation belongs to its run, and
 *  a plan belongs to the project it was named after -- which is what
 *  `projectPage` settles, since a plan's URL is an ordinary chat's. */
function sectionFor(route: Route, projectPage: boolean): RailSection {
  if (route.kind === "chat")
    return route.runId ? "workflows" : route.plan || projectPage ? "projects" : "chats";
  return "workflows";
}

/** `/plan` is where a plan starts, not where it lives.
 *
 *  Once the conversation exists it has a URL of its own, and taking it means a
 *  refresh reopens the plan being written rather than starting a second empty
 *  one. Replaced rather than pushed: Back belongs to whatever page sent you
 *  here, and there is nothing at `/plan` to return to. */
function PlanPermalink() {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  useEffect(() => {
    if (remoteId) window.history.replaceState(null, "", `/conversations/${remoteId}`);
  }, [remoteId]);
  return null;
}

function useProjects() {
  const [projects, setProjects] = useState<ApiProject[]>([]);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<{ projects: ApiProject[] }>("/api/projects")
        .then((value) => {
          if (!cancelled) setProjects(value.projects);
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled) timer = window.setTimeout(load, 1000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);
  return projects;
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
  const projects = useProjects();

  // Settled for this mount: the route is read once, and every move between
  // pages here is a full page load.
  const plan = route.kind === "chat" && Boolean(route.plan);

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        // The plan page is the new chat page with its agent already chosen.
        setAgentId(newChatAgent(value, plan));
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [plan]);

  if (error)
    return <main className="state state-fatal">Could not connect to openengine: {error}</main>;
  if (!config || !agentId || !runner)
    return <main className="state">Starting openengine…</main>;

  const chat = route.kind === "chat";
  // Switching the open conversation in place is for the rail beside a chat of
  // its own. A workflow step's transcript is reached through its run, so
  // leaving one is a move to another page rather than a swap under the URL.
  // The plan page is the other exception: its defaults are a plan's, so "+ New
  // chat" there has to leave rather than quietly start a second planner.
  const standaloneChat = chat && !route.runId && !plan;
  const activeRunId = route.kind === "run" || route.kind === "chat" ? route.runId : undefined;
  // The conversation on screen, and the only thing that says whether it is a
  // project's: a plan's URL is an ordinary chat's, so the projects list is what
  // tells them apart. It arrives after the first paint, and the rail follows.
  const conversationUrl = chat ? window.location.pathname.replace(/\/$/, "") : undefined;
  const projectPage =
    conversationUrl !== undefined &&
    projects.some((project) => project.conversationUrl === conversationUrl);
  return (
    <EngineRuntimeProvider
      defaults={{ agentId, runner, createProject: plan }}
      initialThreadId={chat ? route.threadId : undefined}
      rememberActiveThread={chat}
      // The plan page opens on a new conversation rather than the last one:
      // a New Project button that handed you back the chat you were in would not be
      // a plan. What it starts is still an ordinary chat to come back to.
      restoreActiveThread={chat && !plan}
    >
      <div className="app-shell">
        {plan && <PlanPermalink />}
        <Sidebar
          projects={projects}
          runs={runs}
          initialSection={sectionFor(route, projectPage)}
          linkChats={!standaloneChat}
          activeRunId={activeRunId}
          // Every conversation page, not just a workflow's: a project is one of
          // these too, and the rail marks the one you are in. A standalone
          // chat's path can equal no step's conversation URL, so widening this
          // leaves the workflow rows reading exactly as they did.
          activeConversationUrl={conversationUrl}
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
