import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiThread } from "./api";
import { ChatApp } from "./app";
import { ChatThread } from "./chat";
import { EngineRuntimeProvider } from "./runtime";

type Created = { agentId: string; runner: string };

function json(value: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function runStream() {
  return new Response(
    new TextEncoder().encode(
      `${JSON.stringify({ type: "done", content: [{ type: "text", text: "Noted." }] })}\n`,
    ),
    { headers: { "Content-Type": "application/x-ndjson" } },
  );
}

/** Enough of the chat API to create a conversation and send one turn to it,
 *  recording what every conversation was created on. */
function stubChatApi(agents: string[]) {
  const created: Created[] = [];
  const threads: ApiThread[] = [];

  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";

    if (path === "/api/config")
      return json({
        agents: agents.map((id) => ({ id, description: id, instructions: "" })),
        runners: [{ id: "claude", implementation: "ClaudeCodeAgentRunner" }],
        defaultAgent: "coder",
        defaultRunner: "claude",
        workflowRunners: ["claude"],
        defaultWorkflowRunner: "claude",
      });

    if (path === "/api/threads" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as Created;
      created.push(body);
      const thread: ApiThread = {
        id: `thread-${created.length}`,
        title: "",
        archived: false,
        agentId: body.agentId,
        runner: body.runner,
        workspaceAttached: false,
      };
      threads.push(thread);
      return json(thread);
    }
    if (path === "/api/threads") return json({ threads });
    if (path.endsWith("/title")) return json({ title: "A conversation" });
    if (path.endsWith("/runs") && method === "POST") return runStream();
    if (path.endsWith("/messages"))
      return json({ messages: [], approvals: [], unstable_resume: false });

    const thread = threads.find((candidate) => path === `/api/threads/${candidate.id}`);
    if (thread) return json(thread);
    return json({ error: `not stubbed: ${method} ${path}` }, { status: 404 });
  });

  return { created, fetch };
}

async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(await screen.findByRole("textbox", { name: "Message the agent" }), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("ChatApp", () => {
  it("opens a conversation on the project manager, from whatever was open", async () => {
    const { created, fetch } = stubChatApi(["coder", "project-manager"]);
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<ChatApp />);

    // An ordinary chat first, so the button has to leave one to open its own.
    await send(user, "Help me fix this bug");
    await waitFor(() => expect(created).toEqual([{ agentId: "coder", runner: "claude" }]));

    await user.click(screen.getByRole("button", { name: "Project Manager" }));
    // Only the new-chat heading offers the agent as a choice, so reading
    // project-manager back off it says both that a new conversation is open and
    // what it will be started on.
    expect(await screen.findByRole("combobox", { name: "Agent" })).toHaveValue(
      "project-manager",
    );
    await send(user, "Plan the next milestone");

    await waitFor(() => expect(created).toHaveLength(2));
    expect(created[1]).toEqual({ agentId: "project-manager", runner: "claude" });
  });

  it("keeps the project manager to the conversation it was chosen for", async () => {
    const { created, fetch } = stubChatApi(["coder", "project-manager"]);
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Project Manager" }));
    await send(user, "Plan the next milestone");
    await waitFor(() => expect(created).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: "+ New chat" }));
    expect(await screen.findByRole("combobox", { name: "Agent" })).toHaveValue("coder");
    await send(user, "Help me fix this bug");

    await waitFor(() => expect(created).toHaveLength(2));
    expect(created[1]).toEqual({ agentId: "coder", runner: "claude" });
  });

  it("drops the project manager when a plain new chat is asked for instead", async () => {
    // Changed their mind before sending, so nothing has been created yet and
    // there is no conversation for the choice to have been spent on.
    const { created, fetch } = stubChatApi(["coder", "project-manager"]);
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Project Manager" }));
    await user.click(screen.getByRole("button", { name: "+ New chat" }));
    expect(await screen.findByRole("combobox", { name: "Agent" })).toHaveValue("coder");
    await send(user, "Help me fix this bug");

    await waitFor(() => expect(created).toEqual([{ agentId: "coder", runner: "claude" }]));
  });

  it("reports the conversation a one-shot choice was spent on", async () => {
    // What makes the choice one-shot: creation is the boundary, and the page
    // only knows it has been crossed because the runtime says so.
    const { fetch } = stubChatApi(["coder", "project-manager"]);
    vi.stubGlobal("fetch", fetch);
    const chatCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <EngineRuntimeProvider
        defaults={{ agentId: "project-manager", runner: "claude" }}
        onChatCreated={chatCreated}
      >
        <ChatThread />
      </EngineRuntimeProvider>,
    );

    await send(user, "Plan the next milestone");

    await waitFor(() => expect(chatCreated).toHaveBeenCalledOnce());
  });

  it("offers no project manager when the server ships none", async () => {
    const { fetch } = stubChatApi(["coder"]);
    vi.stubGlobal("fetch", fetch);
    render(<ChatApp />);

    expect(await screen.findByRole("button", { name: "+ New chat" })).toBeVisible();
    expect(screen.queryByText("Projects")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Project Manager" })).not.toBeInTheDocument();
  });
});
