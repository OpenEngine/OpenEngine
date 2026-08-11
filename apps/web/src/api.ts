import type { ThreadMessage } from "@assistant-ui/react";

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
};

export type ApiThread = {
  id: string;
  title: string;
  archived: boolean;
  agentId: string;
  runner: string;
  workspaceRoot?: string;
};

export type ApiMessage = {
  id: string;
  role: "user" | "assistant";
  content: ThreadMessage["content"];
};

export type ApiHistory = {
  messages: ApiMessage[];
  unstable_resume: boolean;
  version: number;
  runStatus: "idle" | "running" | "failed" | "cancelled";
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
