import type { ThreadMessage } from "@assistant-ui/react";

export const RUN_NOT_STARTED_ERROR_CODE = "run-not-started";

/** Mark a submission failure that happened before the server accepted the turn. */
export function runNotStartedError(error: unknown) {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "The agent run could not be started.";
  return { code: RUN_NOT_STARTED_ERROR_CODE, message };
}

export type AgentOption = {
  id: string;
  description: string;
  instructions: string;
};

export type RunnerOption = {
  id: string;
  implementation: string;
};

export type EngineConfig = {
  agents: AgentOption[];
  runners: RunnerOption[];
  defaultAgent: string;
  /** The agent the New Project button talks to, empty when none is composed. */
  planAgent: string;
  defaultRunner: string;
  /** Each workflow declares the inputs its creation form asks for. */
  workflows: {
    id: string; name: string;
    inputs?: { name: string; label: string; default: string; required: boolean; choices: string[] }[];
  }[];
};

/** Which agent the next conversation starts on, for a plan or an ordinary chat.
 *
 *  A deployment that composed no planner says so with an empty `planAgent`, and
 *  the plan page then opens on the default rather than on nothing: the button
 *  starting a chat you can retarget is better than one that starts none. */
export function newChatAgent(config: EngineConfig, plan: boolean): string {
  return (plan && config.planAgent) || config.defaultAgent;
}

export type ApiThread = {
  id: string;
  title: string;
  archived: boolean;
  agentId: string;
  runner: string;
  /** The checkout, when this chat currently has one. */
  workspaceRoot?: string;
  /** What to check out to read this chat's work, attached or not. */
  workspaceRef?: string;
  workspaceAttached: boolean;
};

export type ApiProject = {
  projectId: string;
  name: string;
  /** Put away rather than deleted: listed under Archived, and restorable. */
  archived: boolean;
  /** The planning conversation this project was named after, when it still has
   *  one. A project with none is listed but has nothing to open. */
  conversationUrl?: string;
  /** How many milestones the plan holds, from the responses that counted them.
   *  Absent where nothing counted, which is not the same as a plan of none. */
  milestoneCount?: number;
};

/** The page listing one project's plan in full. */
export function projectMilestonesUrl(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/milestones`;
}

/** One milestone's own page: the workstreams under it and the tasks in each.
 *
 *  Nested under the plan it belongs to rather than named by its id alone: the
 *  page is read as part of a project, and the way back out is the plan. */
export function milestoneDetailsUrl(
  projectId: string,
  milestoneId: string,
): string {
  return `${projectMilestonesUrl(projectId)}/${encodeURIComponent(milestoneId)}`;
}

export function milestoneNewTaskUrl(
  projectId: string,
  milestoneId: string,
): string {
  return `${milestoneDetailsUrl(projectId, milestoneId)}/tasks/new`;
}

export function milestoneScopeUrl(
  projectId: string,
  milestoneId: string,
): string {
  return `${milestoneDetailsUrl(projectId, milestoneId)}/scope`;
}

export type ApiWorkstream = {
  workstreamId: string;
  name: string;
  /** The part of the milestone this workstream covers. */
  scope: string;
};

export type ApiMilestone = {
  milestoneId: string;
  name: string;
  description: string;
  dependencies: string[];
  workstreams: ApiWorkstream[];
};

export type ApiProjectMilestones = {
  project: ApiProject;
  milestones: ApiMilestone[];
};

export type ApiWorkOrderSpec = {
  milestoneId: string;
  name: string;
  objective: string;
  evidenceRequirements: string[];
  dependencies: string[];
};

export type ApiScopingPlan = {
  create: ApiWorkOrderSpec[];
  cancel: string[];
  supersede: { workorderId: string; replacements: ApiWorkOrderSpec[] }[];
  reasons: string[];
};

export function scopeMilestone(
  projectId: string,
  milestoneId: string,
  message: string,
): Promise<ApiScopingPlan> {
  return api<ApiScopingPlan>(
    `/api/projects/${encodeURIComponent(projectId)}/milestones/${encodeURIComponent(milestoneId)}/scope`,
    { method: "POST", body: JSON.stringify({ message }) },
  );
}

export function getProjectMilestones(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiProjectMilestones> {
  return api<ApiProjectMilestones>(
    `/api/projects/${encodeURIComponent(projectId)}/milestones`,
    {
      signal,
    },
  );
}

export function createProject(
  name: string,
  signal?: AbortSignal,
): Promise<ApiProject> {
  return api<ApiProject>("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
    signal,
  });
}

/** Put a project away, or take it back out. */
export function setProjectArchived(
  projectId: string,
  archived: boolean,
): Promise<ApiProject> {
  return api<ApiProject>(
    `/api/projects/${encodeURIComponent(projectId)}/${archived ? "archive" : "unarchive"}`,
    { method: "POST" },
  );
}

/** One node of the graph a WorkOrder is running, as its page draws it.
 *
 *  Built by the page from the graph's topology and the engine's snapshot -- see
 *  `RunView` -- rather than served by this application. */
export type ApiRunStep = {
  stepId: string;
  name: string;
  kind: "agent" | "human";
  status: string;
  outcome: string | null;
  agentId: string | null;
  conversationUrl: string | null;
  waiting: boolean;
  /** Related graph nodes shown together on the WorkOrder overview. */
  group?: string;
  summary: string;
  outputs: { name: string; value: string }[];
};

/** One WorkOrder as `GET /api/runs` lists it.
 *
 *  Every screen polls that list once a second to keep the rail current, so it
 *  carries what a rail, a card and a milestone's task list read and no more.
 *  The prose an agent wrote is the WorkOrder page's, and comes with the single
 *  run it fetches. */
export type ApiWorkflowRunListing = {
  runId: string;
  name: string;
  workflowId: string;
  workflowName: string;
  taskId: string;
  workstreamId: string | null;
  milestoneId: string | null;
  repository: string;
  repositoryContext: { repository: string };
  phase: string;
  /** Live graph frontier supplied by the polled runs list. */
  graphProgress?: {
    activeNodeIds: string[];
    waitingNodeIds: string[];
    nextNodeIds: string[];
  };
  terminalOutcome: string | null;
};

/** One whole WorkOrder, as `GET /api/runs/{runId}` answers for the page about
 *  it: the listing, plus the prose only its own page draws. */
export type ApiWorkflowRun = ApiWorkflowRunListing & {
  taskPrompt: string;
  failureReason: string;
};

/** A WorkOrder as its page draws it: the row, with the stages, frontier and
 *  pending decision read off the graph engine's own snapshot.
 *
 *  Derived rather than served. The row knows identity and lifecycle; what the
 *  run is doing right now belongs to the engine running it, and the page joins
 *  the two rather than asking this application to keep a copy in step. */
export type RunView = ApiWorkflowRun & {
  currentStepId: string | null;
  steps: ApiRunStep[];
  pendingHumanReview: {
    stepId: string;
    title: string;
    prUrl: string | null;
  } | null;
};

export type ApiGraphRun = {
  autoApproveNodes?: string[];
  runnerOverrides?: Record<string, string>;
  runId: string;
  graphId: string;
  status: "running" | "awaiting_approval" | "completed" | "failed";
  activeExecutions: { executionId: string; nodeId: string }[];
  nextNodes: string[];
  values: Record<string, unknown>;
  pendingApprovals: {
    approvalId: string;
    nodeId: string;
    reason: string;
    /** Only a request still open says what may be answered, which is why a
     *  conversation reads its open questions from here rather than from the
     *  event that raised them. */
    allowedDecisions: string[];
    kind?: string;
    command?: string;
    toolName?: string;
  }[];
  error: string;
};

export type ApiGraphTopology = {
  graphId: string;
  nodes: {
    nodeId: string;
    name: string;
    kind: string;
    /** Whether the rail should offer this node's conversation. Absent means
     *  shown: a node that says nothing is one a person can go and read. */
    showInSidebar?: boolean;
    /** Whether steering can restart this node when it is no longer active. */
    alwaysOpen?: boolean;
    /** Related nodes shown together in navigation and WorkOrder summaries. */
    group?: string;
    runner?: string;
    runners?: string[];
  }[];
};

/** The nodes of one graph, which is the same before a run of it has started.
 *
 *  A compiled graph's shape does not change while the server is up, so a client
 *  reads this once per graph rather than per run. */
export function getGraphTopology(
  graphId: string,
  signal?: AbortSignal,
): Promise<ApiGraphTopology> {
  return api<ApiGraphTopology>(
    `/graph/api/graphs/${encodeURIComponent(graphId)}`,
    { signal },
  );
}

/** Where one graph node's conversation is read and steered.
 *
 *  `graph--` rather than a thread id, because nothing about a node's transcript
 *  is a thread: it is folded from the run's events. See `routeForPath`. */
export function graphConversationUrl(runId: string, nodeId: string): string {
  return `/runs/${encodeURIComponent(runId)}/conversations/graph--${encodeURIComponent(nodeId)}`;
}

export type ApiGraphEvent = {
  sequence: number;
  type: string;
  nodeId: string | null;
  payload: Record<string, unknown>;
};

/** Everything the graph engine has said about a run so far.
 *
 *  A finite snapshot of the same feed `/graph/api/runs/{run}/events` streams,
 *  because a page that opens after an agent has finished still has to be able
 *  to read what it did. */
export function getGraphEvents(
  runId: string,
  signal?: AbortSignal,
): Promise<{ events: ApiGraphEvent[] }> {
  return api<{ events: ApiGraphEvent[] }>(
    `/api/runs/${encodeURIComponent(runId)}/graph-events`,
    { signal },
  );
}

export function getGraphRun(
  runId: string,
  signal?: AbortSignal,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(`/graph/api/runs/${encodeURIComponent(runId)}`, {
    signal,
  });
}

/** Retry a node from the latest checkpoint that was about to run it. */
export function retryGraphNode(runId: string, nodeId: string): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/transitions`,
    { method: "POST", body: JSON.stringify({ node: nodeId }) },
  );
}

export function setGraphRunner(
  runId: string,
  nodeId: string,
  runner: string,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/runner`,
    { method: "PATCH", body: JSON.stringify({ node: nodeId, runner }) },
  );
}

export function setGraphAutoApprove(
  runId: string,
  nodeId: string,
  autoApprove: boolean,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/auto-approve`,
    { method: "PATCH", body: JSON.stringify({ node: nodeId, autoApprove }) },
  );
}

/** Stop a graph run, cancelling whatever its nodes are doing.
 *
 *  The graph engine cancels every execution and settles every open request, so
 *  nothing is left driving the run afterwards. The snapshot comes back as
 *  `failed` with a cancellation error. */
export function stopGraphRun(runId: string): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  );
}

/** Steer a node's live agent or reopen a node that allows it.
 *
 *  Addressed to the node rather than to the run: a graph may have several
 *  agents working at once, and the conversation on screen is one of them. The
 *  engine refuses idle nodes unless their topology marks them always open;
 *  those resume from their checkpoint with the message queued. */
export function steerGraphRun(
  runId: string,
  nodeId: string,
  message: string,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/steering`,
    { method: "POST", body: JSON.stringify({ message, node: nodeId }) },
  );
}

/** Answer a request the graph run stopped on, and get the run back as it left
 *  it: deciding is what releases the execution, so the two change together. */
export function decideGraphApproval(
  runId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<ApiGraphRun> {
  return api<ApiGraphRun>(
    `/graph/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
}

/** Throw a WorkOrder away for good.
 *
 *  Unlike archiving a project there is nothing to restore afterwards: the run
 *  and its history go with it, which is why the rail asks first. */
export function deleteRun(runId: string): Promise<void> {
  return api<void>(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
  });
}

/** Choose the runner that answers this conversation from now on.
 *
 * Active workflow conversations restart their current turn on the new runner.
 */
export function setThreadRunner(
  threadId: string,
  runner: string,
): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify({ runner }),
  });
}

export function attachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, {
    method: "POST",
  });
}

export function detachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, {
    method: "DELETE",
  });
}

/** The three answers the product offers. A request may permit fewer: the
 *  provider decides what it can honour, and `allowedDecisions` says so. */
export type ApprovalDecision = "accept" | "accept_for_session" | "cancel";

/** One request for consent, exactly as the server persisted it.
 *
 *  Whole snapshots rather than diffs, because a browser that reconnects
 *  mid-pause has nothing to apply a diff to. */
export type ApiApproval = {
  id: string;
  status: "pending" | "decided" | "interrupted";
  kind:
    | "command_execution"
    | "file_change"
    | "tool_use"
    | "plan_approval"
    | "user_input";
  reason: string | null;
  command: string | null;
  cwd: string | null;
  toolName: string | null;
  /** The tool call this request is about, when the provider named one.
   *
   *  What lets the card sit beside the command it concerns. Null for a request
   *  the provider tied to no call, and for anything recorded before the pairing
   *  existed; those belong to the turn rather than to any one call in it. */
  toolCallId: string | null;
  arguments: string | null;
  allowedDecisions: ApprovalDecision[];
  decision: ApprovalDecision | null;
  decisionSource: "user" | "session_grant" | "policy" | null;
  questions?: ApiQuestion[];
  answers?: Record<string, string[]>;
};

export type ApiQuestion = {
  id: string;
  header: string;
  question: string;
  options: { label: string; description: string }[];
  multiSelect: boolean;
  allowsOther: boolean;
};

/** Answer the request this conversation's run is paused on.
 *
 *  Its own request rather than a reply on the stream that showed it: the
 *  connection that presented the pause may be long gone. */
export function decideApproval(
  threadId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<{ approval: ApiApproval }> {
  return api<{ approval: ApiApproval }>(
    `/api/threads/${threadId}/runs/current/approvals/${approvalId}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
}

export function answerQuestion(
  threadId: string,
  approvalId: string,
  answers: Record<string, string[]>,
): Promise<{ approval: ApiApproval }> {
  return api<{ approval: ApiApproval }>(
    `/api/threads/${threadId}/runs/current/approvals/${approvalId}`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}

/** Stop the run outright. The server cancels whatever it was waiting on first,
 *  which is the approval card's Cancel by another route. */
export function stopRun(threadId: string): Promise<void> {
  return api<void>(`/api/threads/${threadId}/runs/current`, {
    method: "DELETE",
  });
}

export type ApiMessage = {
  id: string;
  role: "user" | "assistant";
  content: ThreadMessage["content"];
};

export type ApiHistory = {
  messages: ApiMessage[];
  /** Everything this conversation has been asked to allow, oldest first. Sent
   *  with the transcript because it outlives the run that raised it. */
  approvals: ApiApproval[];
  unstable_resume: boolean;
};

export type AuthStatus = {
  authenticated: boolean;
  user: { id: number; login: string } | null;
  loginRequired: boolean;
};

export function getAuthStatus(): Promise<AuthStatus> {
  return api<AuthStatus>("/api/auth/github/status");
}

export function logout(): Promise<void> {
  return api<void>("/api/auth/github/logout", { method: "POST" });
}

export type GitHubStatus = { connected: boolean; clientIdConfigured: boolean };

export type SourceControlStatus = {
  provider: "gh-cli" | "github-oauth" | "gitlab-oauth";
  autoSelected: boolean;
  ghCli: {
    installed: boolean;
    authenticated: boolean;
    account: string;
    message: string;
  };
};

export type SourceControlProviderStatus = {
  provider: "gh-cli" | "github-oauth" | "gitlab-oauth";
  autoSelected: boolean;
};

export type GitHubClientIdInfo =
  | { source: "environment" | "configuration"; hint: string }
  | { source: "keychain"; hint: string }
  | { source: "none"; hint: "" };

export type GitHubConnectResponse = {
  userCode: string;
  verificationUri: string;
  expiresIn: number;
  interval: number;
};

export type GitHubPollResponse =
  | { status: "complete" }
  | {
      status: "pending";
      /** Seconds to wait before the next poll. Grows when GitHub returns slow_down. */
      nextInterval: number;
    };

export function getGitHubStatus(): Promise<GitHubStatus> {
  return api<GitHubStatus>("/api/github/status");
}

export function getSourceControlStatus(): Promise<SourceControlStatus> {
  return api<SourceControlStatus>("/api/source-control/status");
}

export function getSourceControlProvider(): Promise<SourceControlProviderStatus> {
  return api<SourceControlProviderStatus>("/api/source-control/provider");
}

export function setSourceControlProvider(
  provider: "gh-cli" | "github-oauth" | "gitlab-oauth",
  origin?: string,
): Promise<void> {
  return api<void>("/api/source-control/provider", {
    method: "POST",
    body: JSON.stringify({ provider, origin }),
  });
}

export function getGitHubClientId(): Promise<GitHubClientIdInfo> {
  return api<GitHubClientIdInfo>("/api/github/client-id");
}

export function setGitHubClientId(clientId: string): Promise<void> {
  return api<void>("/api/github/client-id", {
    method: "POST",
    body: JSON.stringify({ clientId }),
  });
}

export function connectGitHub(): Promise<GitHubConnectResponse> {
  return api<GitHubConnectResponse>("/api/github/connect", { method: "POST" });
}

export function pollGitHubConnect(): Promise<GitHubPollResponse> {
  return api<GitHubPollResponse>("/api/github/connect/poll", {
    method: "POST",
  });
}

export function disconnectGitHub(): Promise<void> {
  return api<void>("/api/github/disconnect", { method: "POST" });
}

export type GitLabStatus = {
  origin: string;
  connected: boolean;
  clientIdConfigured: boolean;
};

export type GitLabDeviceFlow = {
  origin: string;
  userCode: string;
  verificationUri: string;
  expiresIn: number;
  interval: number;
};

export function getGitLabStatus(origin = "https://gitlab.com"): Promise<GitLabStatus> {
  return api<GitLabStatus>(`/api/gitlab/status?origin=${encodeURIComponent(origin)}`);
}

export function setGitLabClientId(origin: string, clientId: string): Promise<void> {
  return api<void>("/api/gitlab/client-id", {
    method: "POST",
    body: JSON.stringify({ origin, clientId }),
  });
}

export function connectGitLab(origin: string): Promise<GitLabDeviceFlow> {
  return api<GitLabDeviceFlow>("/api/gitlab/connect", {
    method: "POST",
    body: JSON.stringify({ origin }),
  });
}

export function pollGitLabConnect(origin: string): Promise<{ status: "complete" | "pending"; nextInterval?: number }> {
  return api("/api/gitlab/connect/poll", {
    method: "POST",
    body: JSON.stringify({ origin }),
  });
}

export function disconnectGitLab(origin: string): Promise<void> {
  return api<void>("/api/gitlab/disconnect", {
    method: "POST",
    body: JSON.stringify({ origin }),
  });
}

/**
 * `events` is whether a mention could start a work order right now, and
 * `signingSecret` is which of its two halves this server already has.
 */
export type SlackStatus = {
  configured: boolean;
  connected: boolean;
  events?: boolean;
  signingSecret?: boolean;
};

export function getSlackStatus(): Promise<SlackStatus> {
  return api<SlackStatus>("/api/slack/status");
}

export function setSlackCredentials(
  clientId: string,
  clientSecret: string,
  signingSecret?: string,
): Promise<void> {
  return api<void>("/api/slack/credentials", {
    method: "POST",
    body: JSON.stringify({ clientId, clientSecret, signingSecret }),
  });
}

/**
 * Save only the signing secret, against the app already configured. Separate
 * from `setSlackCredentials` because that one revokes the token and starts the
 * OAuth flow over, which is not a price for enabling mentions.
 */
export function setSlackSigningSecret(signingSecret: string): Promise<void> {
  return api<void>("/api/slack/credentials", {
    method: "POST",
    body: JSON.stringify({ signingSecret }),
  });
}

export function connectSlack(): Promise<{ authorizationUrl: string }> {
  return api<{ authorizationUrl: string }>("/api/slack/connect", { method: "POST" });
}

export function disconnectSlack(): Promise<void> {
  return api<void>("/api/slack/disconnect", { method: "POST" });
}

/** Where the utilization page lives, which the rail's graph icon opens. */
export const UTILIZATION_URL = "/utilization";

/** One limit a provider meters a subscription against.
 *
 *  `usedPercent` is the provider's own figure. `resetsAt` is an instant, or
 *  empty for a window the provider gave no reset for. */
export type ApiUtilizationWindow = {
  windowId: string;
  label: string;
  usedPercent: number;
  resetsAt: string;
};

/** One runner's reading, taken or attempted.
 *
 *  `error` and windows can both be set: a scrape that failed keeps the figures
 *  the last one found, so the page says what it knows and why it is not newer.
 *  `readAt` is epoch seconds, and zero for a runner never read. */
export type ApiRunnerUtilization = {
  runner: string;
  plan: string;
  windows: ApiUtilizationWindow[];
  error: string;
  /** The command that would fix `error`, where one would. Empty for a failure
   *  nothing on this machine can do anything about, like an unreachable
   *  provider. */
  remedy: string;
  readAt: number;
};

/** The last reading, answered from the cache without touching a provider. */
export function getUtilization(
  signal?: AbortSignal,
): Promise<{ runners: ApiRunnerUtilization[] }> {
  return api<{ runners: ApiRunnerUtilization[] }>("/api/utilization", { signal });
}

/** Ask every runner's provider again, and remember what came back. */
export function refreshUtilization(
  signal?: AbortSignal,
): Promise<{ runners: ApiRunnerUtilization[] }> {
  return api<{ runners: ApiRunnerUtilization[] }>("/api/utilization/refresh", {
    method: "POST",
    signal,
  });
}

/** A refusal, carrying the status it was refused with.
 *
 *  The message is what a reader is shown and is unchanged, so nothing that
 *  catches an `Error` has to know about this. The status is for the callers
 *  that have to tell "there is no such thing" from "the server could not say
 *  right now" -- the two are the same sentence and mean opposite things about
 *  whether the state a page is holding is still good. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      body.error ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function messageText(message: { content: unknown }): string {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter(
      (part): part is { type: "text"; text: string } =>
        typeof part === "object" &&
        part !== null &&
        "type" in part &&
        part.type === "text" &&
        "text" in part &&
        typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("\n");
}
