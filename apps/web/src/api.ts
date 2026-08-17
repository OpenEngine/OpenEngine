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
  defaultRunner: string;
  workflowRunners: string[];
  defaultWorkflowRunner: string;
};

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
  workflowRunId?: string;
  workflowStepId?: string;
};

export type ApiRunStep = {
  stepId: string;
  name: string;
  kind: "agent" | "human";
  status: string;
  outcome: string | null;
  summary: string;
  outputs: { name: string; value: string }[];
  changesRequested: boolean;
  agentId: string | null;
  agentInstanceId: string | null;
  agentRunId: string | null;
  conversationId: string | null;
  conversationUrl: string | null;
};

export type ApiWorkflowRun = {
  runId: string;
  workflowId: string;
  workflowName: string;
  workflowVersion: string;
  taskId: string;
  taskPrompt: string;
  repository: string;
  repositoryContext: { repository: string };
  phase: string;
  currentStepId: string | null;
  terminalOutcome: string | null;
  failureReason: string;
  steps: ApiRunStep[];
  pendingHumanReview: { stepId: string; title: string; summary: string } | null;
  humanDecision: {
    stepId: string;
    approved: boolean;
    outcome: "approved" | "rejected";
    summary: string;
  } | null;
};

/** Choose the runner that answers this conversation from now on. */
export function setThreadRunner(threadId: string, runner: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify({ runner }),
  });
}

export function attachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, { method: "POST" });
}

export function detachWorkspace(threadId: string): Promise<ApiThread> {
  return api<ApiThread>(`/api/threads/${threadId}/workspace`, { method: "DELETE" });
}

export type ApiMessage = {
  id: string;
  role: "user" | "assistant";
  content: ThreadMessage["content"];
};

export type ApiHistory = {
  messages: ApiMessage[];
  unstable_resume: boolean;
};

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
    throw new Error(body.error ?? `${response.status} ${response.statusText}`);
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
