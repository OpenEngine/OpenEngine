import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiWorkflowRun, EngineConfig } from "./api";
import {
  conversationCount,
  NewWorkflowPage,
  phaseAccent,
  phaseLabel,
  RunsPage,
} from "./runs";

const config: EngineConfig = {
  agents: [],
  runners: [],
  defaultAgent: "agent",
  defaultRunner: "runner",
  workflowRunners: ["codex"],
  defaultWorkflowRunner: "codex",
};

function run(overrides: Partial<ApiWorkflowRun> = {}): ApiWorkflowRun {
  return {
    runId: "run-1",
    name: "First run",
    workflowId: "implementation-review-v1",
    workflowName: "Implementation review",
    workflowVersion: "v1",
    taskId: "task-1",
    taskPrompt: "Do the work",
    repository: ".",
    repositoryContext: { repository: "." },
    phase: "implementing",
    currentStepId: "implement",
    terminalOutcome: null,
    failureReason: "",
    steps: [
      {
        stepId: "implement",
        name: "Implementation",
        kind: "agent",
        status: "in_progress",
        outcome: null,
        summary: "",
        outputs: [],
        changesRequested: false,
        agentId: "agent",
        agentInstanceId: "instance",
        agentRunId: "agent-run",
        conversationId: "conversation",
        conversationUrl: "/conversations/conversation",
      },
      {
        stepId: "review",
        name: "Review",
        kind: "human",
        status: "pending",
        outcome: null,
        summary: "",
        outputs: [],
        changesRequested: false,
        agentId: null,
        agentInstanceId: null,
        agentRunId: null,
        conversationId: null,
        conversationUrl: null,
      },
    ],
    pendingHumanReview: null,
    humanDecision: null,
    ...overrides,
  };
}

function json(value: unknown, init?: ResponseInit) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function stubPageApi(runs: ApiWorkflowRun[] = []) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/config") return json(config);
    if (path === "/api/runs" && init?.method === "POST")
      return json(run({ runId: "created-run" }));
    if (path === "/api/runs") return json({ runs });
    return json({ error: "not found" }, { status: 404 });
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("run display helpers", () => {
  it("formats phases and accents their significant states", () => {
    expect(phaseLabel("awaiting_human_review")).toBe("awaiting human review");
    expect(phaseAccent("failed")).toBe("flame");
    expect(phaseAccent("pending")).toBe("quiet");
    expect(phaseAccent("preparing_workspace")).toBe("quiet");
    expect(phaseAccent("implementing")).toBeUndefined();
  });

  it("counts only steps with conversations", () => {
    expect(conversationCount(run())).toBe(1);
    expect(conversationCount(run({ steps: [] }))).toBe(0);
  });
});

describe("NewWorkflowPage", () => {
  it("restores a prompt after unmounting and remounting", async () => {
    vi.stubGlobal("fetch", stubPageApi());
    const user = userEvent.setup();
    const first = render(<NewWorkflowPage />);
    const prompt = screen.getByRole("textbox", { name: "Task prompt" });

    await user.type(prompt, "Keep this draft");
    await waitFor(() =>
      expect(window.localStorage.getItem("engine.workflowDraft")).toBe("Keep this draft"),
    );
    first.unmount();
    render(<NewWorkflowPage />);

    expect(screen.getByRole("textbox", { name: "Task prompt" })).toHaveValue(
      "Keep this draft",
    );
  });

  it("clears the saved prompt after creating a run", async () => {
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<NewWorkflowPage />);
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    const submit = screen.getByRole("button", { name: "Create workflow run" });
    await waitFor(() => expect(submit).toBeEnabled());

    await user.click(submit);

    await waitFor(() => expect(window.localStorage.getItem("engine.workflowDraft")).toBeNull());
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("RunsPage", () => {
  it("renders its empty state", async () => {
    vi.stubGlobal("fetch", stubPageApi());
    render(<RunsPage />);

    expect(await screen.findByRole("heading", { name: "No workflow runs yet." })).toBeVisible();
  });

  it("renders stage and chat counts and offers only phases that exist", async () => {
    const runs = [
      run(),
      run({
        runId: "run-2",
        name: "Second run",
        phase: "failed",
        currentStepId: null,
        terminalOutcome: "rejected",
        steps: [],
      }),
    ];
    vi.stubGlobal("fetch", stubPageApi(runs));
    const user = userEvent.setup();
    const { container } = render(<RunsPage />);

    await screen.findByRole("heading", { name: "First run" });
    const firstCard = container.querySelector('.cards a[href="/runs/run-1"]');
    expect(firstCard).not.toBeNull();
    expect(within(firstCard as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(firstCard as HTMLElement).getByText("1")).toBeInTheDocument();
    expect(within(firstCard as HTMLElement).getByText("Implementation")).toBeInTheDocument();

    const filters = screen.getByRole("group", { name: "Filter runs by phase" });
    expect(within(filters).getByRole("button", { name: "implementing" })).toBeInTheDocument();
    expect(within(filters).getByRole("button", { name: "failed" })).toBeInTheDocument();
    expect(within(filters).queryByRole("button", { name: "pending" })).not.toBeInTheDocument();

    await user.click(within(filters).getByRole("button", { name: "failed" }));
    expect(container.querySelectorAll(".cards .card")).toHaveLength(1);
    expect(screen.getByText("1 of 2 shown")).toBeInTheDocument();
  });
});
