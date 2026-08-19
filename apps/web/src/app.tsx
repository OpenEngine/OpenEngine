/** The chat page: a rail of conversations, and one panel showing whichever is
 *  open -- or, when none is, what the next one will be started on. */

import { useAui, useAuiState } from "@assistant-ui/react";
import { useEffect, useState } from "react";

import {
  api,
  PROJECT_MANAGER_AGENT_ID,
  setThreadRunner,
  type ApiThread,
  type EngineConfig,
  type RunnerOption,
} from "./api";
import { ChatSidebar, ChatThread, ConversationStats } from "./chat";
import { EngineRuntimeProvider } from "./runtime";
import { RunConversationNavigation } from "./runs";

export function ChatApp({
  initialThreadId,
  workflowRunId,
}: {
  initialThreadId?: string;
  workflowRunId?: string;
}) {
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");
  // What the next conversation is started on, in two parts: the default, which
  // is the page's until something changes it, and a choice made for one
  // conversation, which is spent on that conversation. The project manager
  // button is the second kind -- were it the first, every later `+ New chat`
  // would quietly be a project manager chat too.
  const [nextAgentId, setNextAgentId] = useState<string | null>(null);

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

  const nextAgentIdOrDefault = nextAgentId ?? agentId;
  const hasProjectManager = config.agents.some(
    (agent) => agent.id === PROJECT_MANAGER_AGENT_ID,
  );

  return (
    <EngineRuntimeProvider
      defaults={{ agentId: nextAgentIdOrDefault, runner }}
      initialThreadId={initialThreadId}
      onChatCreated={() => setNextAgentId(null)}
    >
      <div className="app-shell">
        {workflowRunId && initialThreadId ? (
          <RunConversationNavigation
            runId={workflowRunId}
            conversationUrl={window.location.pathname.replace(/\/$/, "")}
          />
        ) : (
          // The project manager is the new-chat interface with its agent
          // already chosen, so it is the same screen rather than another one.
          <ChatSidebar
            onNewChat={() => setNextAgentId(null)}
            onOpenProjectManager={
              hasProjectManager
                ? () => setNextAgentId(PROJECT_MANAGER_AGENT_ID)
                : undefined
            }
          />
        )}
        <main className="panel">
          <ChatHeader
            config={config}
            agentId={nextAgentIdOrDefault}
            runner={runner}
            onAgentChange={(chosen) => {
              // Chosen here rather than for one conversation, so it is the
              // default from now on and replaces any one-shot choice.
              setNextAgentId(null);
              setAgentId(chosen);
            }}
            onRunnerChange={setRunner}
          />
          <ConversationStats />
          <ChatThread />
        </main>
      </div>
    </EngineRuntimeProvider>
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
