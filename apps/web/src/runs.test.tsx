import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject, ApiWorkflowRun, EngineConfig } from "./api";
import {
  NewWorkflowPage,
  phaseAccent,
  phaseLabel,
  RunDetailPage,
  RunsPage,
  runStatusLabel,
  useGraphNodes,
  useRuns,
} from "./runs";

const config: EngineConfig = {
  agents: [],
  runners: [],
  defaultAgent: "agent",
  planAgent: "planner",
  defaultRunner: "runner",
  workflows: [
    { id: "work-v1", name: "Work" },
    { id: "release-v2", name: "Release" },
  ],
};
/** The same deployment, with a graph workflow beside the step ones. */
const withGraph: EngineConfig = {
  ...config,
  workflows: [
    ...config.workflows,
    {
      id: "implementation-review-codex",
      name: "Implementation review (codex)",
    },
  ],
};
const project: ApiProject = {
  projectId: "project-1",
  name: "Engine roadmap",
  archived: false,
};
const milestone: ApiMilestone = {
  milestoneId: "milestone-foundation",
  name: "Foundation",
  description: "Build the shared model.",
  dependencies: [],
};

function run(overrides: Partial<ApiWorkflowRun> = {}): ApiWorkflowRun {
  return {
    runId: "run-1",
    name: "First run",
    workflowId: "work-v1",
    workflowName: "Work",
    taskId: "task-1",
    milestoneId: null,
    taskPrompt: "Do the work",
    repository: ".",
    repositoryContext: { repository: "." },
    phase: "running_agent",
    terminalOutcome: null,
    failureReason: "",
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
    if (path === "/api/runs" && init?.method === "POST")
      return json(run({ runId: "created-run" }));
    if (path === "/api/runs") return json({ runs });
    return json({ error: "not found" }, { status: 404 });
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("run display helpers", () => {
  it("formats phases and accents their significant states", () => {
    expect(phaseLabel("awaiting_human_review")).toBe("awaiting human review");
    expect(phaseAccent("failed")).toBe("flame");
    expect(phaseAccent("pending")).toBe("quiet");
    expect(phaseAccent("preparing_workspace")).toBe("quiet");
    expect(phaseAccent("running_agent")).toBeUndefined();
  });

  it("labels runs with their lifecycle phase", () => {
    expect(runStatusLabel(run())).toBe("running agent");
    expect(runStatusLabel(run({ phase: "succeeded" }))).toBe("succeeded");
  });
});

describe("NewWorkflowPage", () => {
  it("offers every configured workflow definition", () => {
    render(<NewWorkflowPage config={config} />);

    expect(
      screen.getByText(
        "Create one WorkOrder that keeps its stages, agent conversations, outputs, and final human decision together.",
      ),
    ).toBeVisible();
    const selector = screen.getByRole("combobox", { name: "Workflow definition" });
    expect(within(selector).getByRole("option", { name: "Release" })).toHaveValue(
      "release-v2",
    );
  });

  it("offers a graph workflow by its name alone, with no version to show", async () => {
    // A graph workflow has no version, and "name · " with nothing after it
    // reads like something failed to load.
    render(<NewWorkflowPage config={withGraph} />);

    const selector = screen.getByRole("combobox", { name: "Workflow definition" });
    expect(
      within(selector).getByRole("option", { name: "Implementation review (codex)" }),
    ).toHaveValue("implementation-review-codex");
  });

  it("does not ask which runner to use for a workflow that names its own agent", async () => {
    // The graph decides which agent runs it -- there is one entry per agent --
    // so the server reads no runner for one. A field that appears to choose
    // something and is then ignored is worse than no field.
    const user = userEvent.setup();
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(<NewWorkflowPage config={withGraph} />);
    expect(screen.queryByRole("combobox", { name: "Implementation runner" })).not.toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Workflow definition" }),
      "implementation-review-codex",
    );

    expect(
      screen.queryByRole("combobox", { name: "Implementation runner" }),
    ).not.toBeInTheDocument();

    // And nothing is sent in its place: the WorkOrder carries the graph, the
    // prompt and the repository, which is all a graph is given.
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    await user.click(screen.getByRole("button", { name: "Create WorkOrder" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/runs", expect.anything()));
    const request = fetch.mock.calls.find(([url]) => url === "/api/runs")?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).not.toHaveProperty("runner");
  });

  it("submits independent workflow inputs and resets them when switching workflows", async () => {
    const user = userEvent.setup();
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const configured: EngineConfig = {
      ...withGraph,
      workflows: withGraph.workflows.map((workflow) => workflow.id === "implementation-review-codex" ? {
        ...workflow,
        inputs: [
          { name: "implementation_runner", label: "Implementation runner", default: "codex", required: true, choices: ["codex", "claude"] },
          { name: "review_runner", label: "Review runner", default: "claude", required: true, choices: ["codex", "claude"] },
          { name: "context", label: "Extra context", default: "", required: false, choices: [] },
        ],
      } : workflow),
    };
    render(<NewWorkflowPage config={configured} />);
    const workflow = screen.getByRole("combobox", { name: "Workflow definition" });
    await user.selectOptions(workflow, "implementation-review-codex");
    expect(screen.getByText("Workflow inputs")).toBeVisible();
    await user.selectOptions(screen.getByRole("combobox", { name: "Implementation runner" }), "claude");
    expect(screen.getByRole("combobox", { name: "Review runner" })).toHaveValue("claude");
    await user.selectOptions(workflow, config.workflows[0].id);
    expect(screen.queryByText("Workflow inputs")).not.toBeInTheDocument();
    await user.selectOptions(workflow, "implementation-review-codex");
    expect(screen.getByRole("combobox", { name: "Implementation runner" })).toHaveValue("codex");
    await user.selectOptions(screen.getByRole("combobox", { name: "Implementation runner" }), "claude");
    await user.selectOptions(screen.getByRole("combobox", { name: "Review runner" }), "codex");
    await user.type(screen.getByRole("textbox", { name: "Extra context" }), "Check migrations");
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    await user.click(screen.getByRole("button", { name: "Create WorkOrder" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/runs", expect.anything()));
    const request = fetch.mock.calls.find(([url]) => url === "/api/runs")?.[1] as RequestInit;
    expect(JSON.parse(String(request.body)).inputs).toEqual({
      implementation_runner: "claude", review_runner: "codex", context: "Check migrations",
    });
  });

  it("restores a prompt after unmounting and remounting", async () => {
    vi.stubGlobal("fetch", stubPageApi());
    const user = userEvent.setup();
    const first = render(<NewWorkflowPage config={config} />);
    const prompt = screen.getByRole("textbox", { name: "Task prompt" });

    await user.type(prompt, "Keep this draft");
    await waitFor(() =>
      expect(window.localStorage.getItem("engine.workflowDraft")).toBe("Keep this draft"),
    );
    first.unmount();
    render(<NewWorkflowPage config={config} />);

    expect(screen.getByRole("textbox", { name: "Task prompt" })).toHaveValue(
      "Keep this draft",
    );
  });

  it("clears the saved prompt after creating a run", async () => {
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<NewWorkflowPage config={config} />);
    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Ship it");
    const submit = screen.getByRole("button", { name: "Create WorkOrder" });
    await waitFor(() => expect(submit).toBeEnabled());

    await user.click(submit);

    await waitFor(() => expect(window.localStorage.getItem("engine.workflowDraft")).toBeNull());
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("creates a task under the milestone it was opened from", async () => {
    const fetch = stubPageApi();
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<NewWorkflowPage config={config} project={project} milestone={milestone} />);

    await user.type(screen.getByRole("textbox", { name: "Task prompt" }), "Persist it");
    await user.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/runs", expect.anything()));
    const request = fetch.mock.calls.find(([url]) => url === "/api/runs")?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toMatchObject({
      milestoneId: "milestone-foundation",
    });
  });
});

describe("useRuns", () => {
  it("reads the runs the shell hands to both the page and the rail", async () => {
    vi.stubGlobal("fetch", stubPageApi([run()]));

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.runs).toHaveLength(1));
    expect(result.current.runs[0].runId).toBe("run-1");
    expect(result.current.error).toBe("");
    expect(result.current.loaded).toBe(true);
  });

  it("reports a failure instead of an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ error: "runs unavailable" }, { status: 500 })),
    );

    const { result } = renderHook(() => useRuns());

    await waitFor(() => expect(result.current.error).toBe("runs unavailable"));
    expect(result.current.runs).toEqual([]);
    expect(result.current.loaded).toBe(false);
  });

  it("refreshes terminal runs so reactivated waiting conversations appear", async () => {
    vi.useFakeTimers();
    const terminal = run({
      phase: "succeeded",
      terminalOutcome: "approved",
    });
    const waiting = run({ graphProgress: { activeNodeIds: [], waitingNodeIds: ["review"], nextNodeIds: [] } });
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ runs: [terminal] }))
      .mockResolvedValue(json({ runs: [waiting] }));
    vi.stubGlobal("fetch", fetch);

    const { result } = renderHook(() => useRuns());
    await act(async () => {});
    expect(result.current.runs[0].phase).toBe("succeeded");
    expect(result.current.runs[0].graphProgress).toBeUndefined();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(result.current.runs[0].phase).toBe("running_agent");
    expect(result.current.runs[0].graphProgress?.waitingNodeIds).toEqual(["review"]);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps refreshing after a transient load failure", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("runs unavailable"))
      .mockResolvedValue(json({ runs: [run()] }));
    vi.stubGlobal("fetch", fetch);

    const { result } = renderHook(() => useRuns());
    await act(async () => {});
    expect(result.current.error).toBe("runs unavailable");

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(result.current.runs).toHaveLength(1);
    expect(result.current.error).toBe("");
    expect(result.current.loaded).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  /** Nothing puts a deleted run back, so the click is asked about first and
   *  the row leaves on the answer rather than a poll later. */
  it("deletes a run once the reader confirms, and drops its row at once", async () => {
    const fetch = stubPageApi([run()]);
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    const { result } = renderHook(() => useRuns());
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    await act(async () => result.current.remove(result.current.runs[0]));

    expect(window.confirm).toHaveBeenCalledWith(
      "Delete First run? This cannot be undone.",
    );
    expect(result.current.runs).toEqual([]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/run-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("keeps the run when the reader says no", async () => {
    const fetch = stubPageApi([run()]);
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(false));

    const { result } = renderHook(() => useRuns());
    await waitFor(() => expect(result.current.runs).toHaveLength(1));

    await act(async () => result.current.remove(result.current.runs[0]));

    expect(result.current.runs).toHaveLength(1);
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/runs/run-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});

describe("useGraphNodes", () => {
  const graphRun = () =>
    run({
      runId: "run-2",
      workflowId: "implementation-review-codex",
    });
  const topology = {
    graphId: "implementation-review-codex",
    nodes: [
      { nodeId: "implementation", name: "Implementation", kind: "agent" },
      { nodeId: "human-review", name: "Human review", kind: "human", showInSidebar: false },
    ],
  };

  it("reads the nodes of every graph the WorkOrders on screen run", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) =>
      String(input) === "/graph/api/graphs/implementation-review-codex"
        ? json(topology)
        : json({ error: "not found" }, { status: 404 }),
    );
    vi.stubGlobal("fetch", fetch);

    const { result, rerender } = renderHook(
      ({ runs }) => useGraphNodes(runs),
      { initialProps: { runs: [graphRun(), graphRun()] } },
    );

    await waitFor(() =>
      expect(result.current["implementation-review-codex"]).toHaveLength(2),
    );
    // Multiple runs of the same graph share one topology request.
    expect(fetch).toHaveBeenCalledTimes(1);

    // A poll answering with the same WorkOrders is not news about their graphs,
    // whose shape does not change while the server is up.
    rerender({ runs: [graphRun(), graphRun()] });
    await act(async () => {});
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("offers nothing when the graph engine will not describe a graph", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ error: "not running graph workflows" }, { status: 502 })),
    );

    const { result } = renderHook(() => useGraphNodes([graphRun()]));

    await act(async () => {});
    expect(result.current).toEqual({});
  });
});

describe("RunsPage", () => {
  it("renders its empty state", () => {
    render(<RunsPage runs={[]} error="" />);

    expect(screen.getByRole("heading", { name: "No WorkOrders yet." })).toBeVisible();
  });

  it("reports a load failure over the empty state", () => {
    render(<RunsPage runs={[]} error="runs unavailable" />);

    expect(screen.getByText(/runs unavailable/)).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "No WorkOrders yet." }),
    ).not.toBeInTheDocument();
  });

  it("renders repository and phase and offers only phases that exist", async () => {
    const runs = [
      run(),
      run({
        runId: "run-2",
        name: "Second run",
        phase: "failed",
        terminalOutcome: "rejected",
      }),
    ];
    const user = userEvent.setup();
    const { container } = render(<RunsPage runs={runs} error="" />);

    await screen.findByRole("heading", { name: "First run" });
    const firstCard = container.querySelector('.cards a[href="/runs/run-1"]');
    expect(firstCard).not.toBeNull();
    expect(within(firstCard as HTMLElement).getByText(".")).toBeInTheDocument();
    expect(within(firstCard as HTMLElement).getAllByText("running agent")).toHaveLength(2);

    const filters = screen.getByRole("group", { name: "Filter WorkOrders by phase" });
    expect(within(filters).getByRole("button", { name: "running agent" })).toBeInTheDocument();
    expect(within(filters).getByRole("button", { name: "failed" })).toBeInTheDocument();
    expect(within(filters).queryByRole("button", { name: "pending" })).not.toBeInTheDocument();

    await user.click(within(filters).getByRole("button", { name: "failed" }));
    expect(container.querySelectorAll(".cards .card")).toHaveLength(1);
    expect(screen.getByText("1 of 2 shown")).toBeInTheDocument();
  });

  it("narrows the list by title and says so when nothing matches", async () => {
    const runs = [run(), run({ runId: "run-2", name: "Second run", phase: "failed" })];
    const user = userEvent.setup();
    const { container } = render(<RunsPage runs={runs} error="" />);

    const search = screen.getByRole("searchbox", { name: "Filter WorkOrders by title" });
    await user.type(search, "second");
    expect(container.querySelectorAll(".cards .card")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Second run" })).toBeVisible();
    expect(screen.getByText("1 of 2 shown")).toBeInTheDocument();

    await user.clear(search);
    await user.type(search, "third");
    expect(
      screen.getByRole("heading", { name: "No WorkOrders match this filter." }),
    ).toBeVisible();
    expect(screen.getByText("0 of 2 shown")).toBeInTheDocument();
  });
});

describe("RunDetailPage", () => {
  it.each([
    ["Approve", "accept"],
    ["Reject", "cancel"],
  ])("submits %s to the graph approval and clears the decision", async (label, decision) => {
    const awaiting = {
      runId: "run-1", graphId: "work-v1", status: "awaiting_approval",
      activeExecutions: [], nextNodes: [], values: {}, error: "",
      pendingApprovals: [{ approvalId: "approval-1", nodeId: "review", reason: "Review the release", allowedDecisions: ["accept", "cancel"] }],
    };
    const settled = { ...awaiting, status: "completed", pendingApprovals: [] };
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/approvals/approval-1") && init?.method === "POST") return json(settled);
      if (path === "/api/runs/run-1") return json(run());
      if (path === "/graph/api/runs/run-1") return json(awaiting);
      if (path === "/graph/api/graphs/work-v1") return json({ graphId: "work-v1", nodes: [{ nodeId: "review", name: "Release review", kind: "human" }] });
      return json({ events: [] });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<RunDetailPage runId="run-1" />);
    await user.click(await screen.findByRole("button", { name: label }));
    await waitFor(() => expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument());
    expect(fetch).toHaveBeenCalledWith("/graph/api/runs/run-1/approvals/approval-1", expect.objectContaining({ method: "POST", body: JSON.stringify({ decision }) }));
  });

  it("keeps a graph approval available after a refused decision", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") return json({ error: "decision unavailable" }, { status: 409 });
      if (path === "/api/runs/run-1") return json(run());
      if (path === "/graph/api/runs/run-1") return json({
        runId: "run-1", graphId: "work-v1", status: "awaiting_approval",
        activeExecutions: [], nextNodes: [], values: {}, error: "",
        pendingApprovals: [{ approvalId: "approval-1", nodeId: "review", reason: "Review the release", allowedDecisions: ["accept", "cancel"] }],
      });
      if (path === "/graph/api/graphs/work-v1") return json({ graphId: "work-v1", nodes: [{ nodeId: "review", name: "Release review", kind: "human" }] });
      return json({ events: [] });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(<RunDetailPage runId="run-1" />);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Could not record decision: decision unavailable/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });


  it.each([
    ["https://github.com/example/repo/pull/1", "https://github.com/example/repo/pull/1"],
    ["http://example.com/pull/1", "http://example.com/pull/1"],
    ["javascript:alert(1)", null],
    ["data:text/html,unsafe", null],
  ])("renders structured graph outputs and validates approval URL %s", async (prUrl, expectedUrl) => {
    const graphRun = run({
      workflowId: "implementation-review-codex",
    });
    const values = {
      empty: null,
      planning: "Plan complete",
      implementation: {
        summary: "Implemented the change",
        pr_url: prUrl,
        metadata: { checks: { passed: true } },
        artifacts: ["report", { name: "build" }],
        count: 0,
        approved: false,
        omitted: null,
      },
      later: { pr_url: "javascript:alert(2)" },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1") return json({
        runId: "run-1",
        graphId: graphRun.workflowId,
        status: "awaiting_approval",
        activeExecutions: [],
        nextNodes: [],
        values,
        pendingApprovals: [{ nodeId: "review", reason: "Review the change", allowedDecisions: ["accept", "cancel"] }],
        error: "",
      });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`) return json({
        graphId: graphRun.workflowId,
        nodes: Object.keys(values).map((nodeId) => ({ nodeId, name: nodeId, kind: "agent" })),
      });
      if (path === "/api/runs/run-1/graph-events") return json({
        events: [{ sequence: 1, type: "node.finished", nodeId: "implementation", payload: {} }],
      });
      return json({ error: "not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    const heading = await screen.findByRole("heading", { name: "implementation" });
    const step = within(heading.closest("article")!);
    expect(step.getByText("Implemented the change")).toBeVisible();
    expect(step.getByText('{"checks":{"passed":true}}')).toBeVisible();
    expect(step.getByText('["report",{"name":"build"}]')).toBeVisible();
    expect(step.getByText("0")).toBeVisible();
    expect(step.getByText("false")).toBeVisible();
    expect(step.queryByText("omitted")).not.toBeInTheDocument();
    expect(step.queryByText("summary")).not.toBeInTheDocument();
    expect(screen.getByText("Plan complete")).toBeVisible();
    const empty = screen.getByRole("heading", { name: "empty" }).closest("article")!;
    expect(within(empty).queryByRole("definition")).not.toBeInTheDocument();
    if (expectedUrl) {
      expect(step.getByRole("link", { name: `${prUrl} ↗` })).toHaveAttribute("href", expectedUrl);
      expect(screen.getByRole("link", { name: "View pull request ↗" })).toHaveAttribute("href", expectedUrl);
    } else {
      expect(step.getByText(prUrl!)).toBeVisible();
      expect(step.queryByRole("link")).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "View pull request ↗" })).not.toBeInTheDocument();
    }
  });

  it("says where to look for a WorkOrder that has no stages here", async () => {
    // A graph WorkOrder has no steps to draw, because a graph is not made of
    // them. Without a word of explanation the page reads as one that never
    // started.
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "Implementation review (codex)",
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1")
        return json({
          runId: "run-1",
          graphId: graphRun.workflowId,
          status: "running",
          activeExecutions: [],
          nextNodes: [],
          values: {},
          pendingApprovals: [],
          error: "",
        });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`)
        return json({ graphId: graphRun.workflowId, nodes: [] });
      if (path === "/api/runs/run-1/graph-events") return json({ events: [] });
      return json({ error: "not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText("Stages unavailable")).toBeVisible();
    expect(screen.getByText("Implementation review (codex)")).toBeVisible();
  });

  it("says a WorkOrder cannot be loaded once its workflow is gone", async () => {
    // A WorkOrder outlives the workflow it ran. Once that workflow has been
    // renamed or withdrawn there is no graph to draw its stages from, and the
    // page has to say so rather than throw the whole WorkOrder away over a 404
    // or read as one that never started.
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "implementation-review-codex",
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/api/runs/run-1/graph-events") return json({ events: [] });
      // Both graph reads answer the same way: no such graph.
      return json({ error: "graph not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText(/no longer has/)).toBeVisible();
    expect(screen.getByRole("heading", { name: graphRun.name })).toBeVisible();
    expect(screen.queryByText(/Could not load WorkOrder/)).not.toBeInTheDocument();
  });

  it("opens a graph conversation before its first transcript arrives", async () => {
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "Implementation review (codex)",
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1")
        return json({
          runId: "run-1",
          graphId: graphRun.workflowId,
          status: "running",
          activeExecutions: [{ executionId: "execution-1", nodeId: "implementation" }],
          nextNodes: ["implementation"],
          values: {},
          pendingApprovals: [],
          error: "",
        });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`)
        return json({
          graphId: graphRun.workflowId,
          nodes: [{ nodeId: "implementation", name: "Implementation", kind: "agent" }],
        });
      if (path === "/api/runs/run-1/graph-events")
        return json({
          events: [{
            sequence: 4,
            type: "conversation.started",
            nodeId: "implementation",
            payload: { agent: "codex", sessionId: "session-1", resumed: false },
          }],
        });
      return json({ error: "not found" }, { status: 404 });
    }));

    render(<RunDetailPage runId="run-1" />);

    const link = await screen.findByRole("link", { name: /Open conversation/ });
    expect(link).toHaveAttribute(
      "href",
      "/runs/run-1/conversations/graph--implementation",
    );
    expect(screen.queryByText("Conversation not started")).not.toBeInTheDocument();
  });

  it("collapses related graph agents into one overview group", async () => {
    const graphRun = run({
      workflowId: "implementation-review-codex",
      workflowName: "Implementation review (codex)",
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1")
        return json({
          runId: "run-1",
          graphId: graphRun.workflowId,
          status: "running",
          activeExecutions: [{ executionId: "execution-1", nodeId: "implementation" }],
          nextNodes: ["implementation"],
          values: {},
          pendingApprovals: [],
          error: "",
        });
      if (path === `/graph/api/graphs/${graphRun.workflowId}`)
        return json({
          graphId: graphRun.workflowId,
          nodes: [
            { nodeId: "implementation", name: "Implementation", kind: "agent" },
            {
              nodeId: "review-security",
              name: "Review (Security)",
              kind: "agent",
              group: "Review",
            },
            {
              nodeId: "review-performance",
              name: "Review (Performance)",
              kind: "agent",
              group: "Review",
            },
            { nodeId: "reranker", name: "Reranker", kind: "agent" },
          ],
        });
      if (path === "/api/runs/run-1/graph-events") return json({ events: [] });
      return json({ error: "not found" }, { status: 404 });
    }));
    const user = userEvent.setup();
    const { container } = render(<RunDetailPage runId="run-1" />);

    await screen.findByText("Implementation review (codex)");
    const stages = within(container.querySelector(".stages") as HTMLElement);
    expect(stages.getByText("Review")).toBeVisible();
    expect(stages.queryByText("Review (Security)")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Review (Security)" })).not.toBeVisible();

    await user.click(screen.getByText("Review", { selector: "summary strong" }));

    expect(screen.getByRole("heading", { name: "Review (Security)" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Review (Performance)" })).toBeVisible();
  });

  it("detaches and reattaches a graph workflow's checkout", async () => {
    const graphRun = run({ workflowId: "graph" });
    let attached = true;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/runs/run-1") return json(graphRun);
      if (path === "/graph/api/runs/run-1") return json({
        runId: "run-1", graphId: "graph", status: "completed",
        activeExecutions: [], nextNodes: [], pendingApprovals: [], error: "",
        values: { workspaceId: "ws-1", workspace: "/worktrees/ws-1" },
      });
      if (path === "/graph/api/graphs/graph") return json({ graphId: "graph", nodes: [] });
      if (path === "/graph/api/runs/run-1/workspace") {
        if (init?.method === "DELETE") attached = false;
        if (init?.method === "POST") attached = true;
        return json({
          workspaceAttached: attached, workspaceRef: "engine/ws-1",
          workspaceRoot: attached ? "/worktrees/ws-1" : null,
        });
      }
      return json({ events: [] });
    });
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    const page = render(<RunDetailPage runId="run-1" />);
    expect(await screen.findByText("cd /worktrees/ws-1")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Detach" }));
    expect(await screen.findByText("git checkout engine/ws-1")).toBeVisible();
    page.unmount();
    render(<RunDetailPage runId="run-1" />);
    await user.click(await screen.findByRole("button", { name: "Reattach" }));
    expect(await screen.findByText("cd /worktrees/ws-1")).toBeVisible();
    expect(fetch).toHaveBeenCalledWith(
      "/graph/api/runs/run-1/workspace", expect.objectContaining({ method: "POST" }),
    );
  });

});
