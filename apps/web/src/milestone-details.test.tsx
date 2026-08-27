import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject, ApiWorkflowRun } from "./api";
import { MilestoneDetailsPage } from "./milestone-details";

const foundation: ApiMilestone = {
  milestoneId: "milestone-foundation",
  name: "Foundation",
  description: "Build the shared project model.",
  dependencies: [],
  workstreams: [
    { workstreamId: "workstream-data", name: "Data model", scope: "Persist the plan." },
    { workstreamId: "workstream-web", name: "Timeline view", scope: "" },
  ],
};
const launch: ApiMilestone = {
  milestoneId: "milestone-launch",
  name: "Launch",
  description: "Ship the project to users.",
  dependencies: ["milestone-foundation"],
  workstreams: [],
};
const project: ApiProject = {
  projectId: "project-1",
  name: "Engine roadmap",
  archived: false,
  conversationUrl: "/conversations/agi-1",
};

/** A run is the record of one task, and carries the workstream it was started
 *  in -- which is all this page groups them by. */
function run(
  runId: string,
  name: string,
  workstreamId: string | null,
  phase = "succeeded",
  milestoneId: string | null = null,
): ApiWorkflowRun {
  return {
    runId,
    name,
    workflowId: "delivery",
    workflowName: "Delivery",
    workflowVersion: "1",
    taskId: `task-${runId}`,
    workstreamId,
    milestoneId,
    taskPrompt: name,
    repository: ".",
    repositoryContext: { repository: "." },
    phase,
    currentStepId: null,
    terminalOutcome: null,
    failureReason: "",
    steps: [],
    pendingHumanReview: null,
    humanDecision: null,
  };
}

const persisting = run("run-1", "Persist milestones", "workstream-data", "running_agent");
const migrating = run("run-2", "Add the workstream table", "workstream-data");
const drawing = run("run-3", "Draw the dependency graph", "workstream-web");
const unplanned = run("run-4", "A chore nobody planned", null);
const direct = run(
  "run-6",
  "Document the milestone",
  null,
  "succeeded",
  "milestone-foundation",
);
const reviewing = run(
  "run-5",
  "Approve the release",
  "workstream-data",
  "awaiting_human_review",
);

/** A fresh response per call: a poll reads more than one of them. */
function plan(milestones: ApiMilestone[]) {
  return async () =>
    new Response(JSON.stringify({ project, milestones }), {
      headers: { "Content-Type": "application/json" },
    });
}

function unavailable() {
  return async () =>
    new Response(JSON.stringify({ error: "store unavailable" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
}

function open(
  milestoneId = "milestone-foundation",
  runs = [persisting, migrating, drawing],
  { runsLoaded = true, runsError = "" } = {},
) {
  return render(
    <MilestoneDetailsPage
      projectId="project-1"
      milestoneId={milestoneId}
      runs={runs}
      runsLoaded={runsLoaded}
      runsError={runsError}
    />,
  );
}

describe("MilestoneDetailsPage", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("names the milestone it was routed to, and offers the plan it came from", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(plan([foundation, launch]));
    vi.stubGlobal("fetch", fetch);

    open("milestone-launch");
    expect(screen.getByText("Loading milestone…")).toBeInTheDocument();

    await act(async () => {});

    expect(fetch.mock.lastCall?.[0]).toBe("/api/projects/project-1/milestones");
    expect(screen.getByRole("heading", { name: "Launch", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Ship the project to users.")).toBeInTheDocument();
    // The dependency reads as the goal it names, not as the id recorded.
    expect(screen.getByText("Depends on Foundation")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← All milestones" })).toHaveAttribute(
      "href",
      "/projects/project-1/milestones",
    );
    expect(screen.getByRole("link", { name: "New task" })).toHaveAttribute(
      "href",
      "/projects/project-1/milestones/milestone-launch/tasks/new",
    );
  });

  it("gives a card to every workstream under the milestone, and to no other", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation, launch])));

    const { container } = open();
    await act(async () => {});

    expect(
      [...container.querySelectorAll(".workstream-card h2")].map((h) => h.textContent),
    ).toEqual(["Data model", "Timeline view"]);
    const data = screen.getByRole("article", { name: "Data model" });
    expect(within(data).getByText("Persist the plan.")).toBeInTheDocument();
    expect(within(data).getByText("workstream-data")).toBeInTheDocument();
  });

  it("lists each workstream's tasks, and leads to the run behind one", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [persisting, migrating, drawing, unplanned]);
    await act(async () => {});

    const data = screen.getByRole("article", { name: "Data model" });
    const tasks = within(data).getByRole("list", { name: "Tasks in Data model" });
    // Grouped by the workstream each run was started in: the task belonging to
    // no workstream is on no card here, and the other milestone's is on its own.
    expect(within(tasks).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "/runs/run-1",
      "/runs/run-2",
    ]);
    expect(within(tasks).getByText("Persist milestones")).toBeInTheDocument();
    // The stage the run has reached, in the workflow's own words while it runs.
    expect(within(tasks).getByText("running agent")).toBeInTheDocument();
    expect(within(tasks).getByText("succeeded")).toBeInTheDocument();
    // A non-terminal run is work left under this heading.
    expect(within(data).getByText("2 tasks")).toBeInTheDocument();
    expect(within(data).getByText("1 unfinished")).toBeInTheDocument();
  });

  it("counts a task awaiting human review as unfinished and calls it out", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [reviewing]);
    await act(async () => {});

    const data = screen.getByRole("article", { name: "Data model" });
    expect(within(data).getByText("1 unfinished")).toBeInTheDocument();
    expect(within(data).getByText("1 awaiting review")).toBeInTheDocument();
    expect(screen.getByText("Awaiting review").nextSibling).toHaveTextContent("1");
  });

  it("lists tasks linked directly to the milestone below its workstreams", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [persisting, direct, unplanned]);
    await act(async () => {});

    const list = screen.getByRole("list", { name: "Tasks in Foundation" });
    expect(within(list).getByRole("link", { name: /Document the milestone/ })).toHaveAttribute(
      "href",
      "/runs/run-6",
    );
    expect(within(list).queryByText("A chore nobody planned")).toBeNull();
    expect(screen.getByText("Tasks").nextSibling).toHaveTextContent("2");
  });

  it("says a workstream nothing has been started in has nothing in it", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", []);
    await act(async () => {});

    const view = screen.getByRole("article", { name: "Timeline view" });
    expect(
      within(view).getByText("No tasks have been started in this workstream yet."),
    ).toBeInTheDocument();
    expect(within(view).getByText("0 tasks")).toBeInTheDocument();
    expect(screen.queryByText("1 unfinished")).toBeNull();
  });

  it("does not claim there are no tasks before the runs poll answers", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [], { runsLoaded: false });
    await act(async () => {});

    expect(screen.getAllByText("Loading tasks…")).toHaveLength(3);
    expect(screen.queryByText(/No tasks have been started/)).toBeNull();
    expect(screen.getByText("Tasks").nextSibling).toHaveTextContent("—");
  });

  it("reports a runs failure instead of rendering a confident empty state", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [], {
      runsLoaded: false,
      runsError: "runs unavailable",
    });
    await act(async () => {});

    expect(screen.getAllByText("Could not load tasks: runs unavailable")).toHaveLength(3);
    expect(screen.queryByText(/No tasks have been started/)).toBeNull();
  });

  it("keeps known tasks on screen and reports a later runs failure", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [persisting], {
      runsLoaded: true,
      runsError: "runs unavailable",
    });
    await act(async () => {});

    expect(screen.getByText("Not updating: runs unavailable")).toBeInTheDocument();
    expect(screen.getByText("Persist milestones")).toBeInTheDocument();
  });

  it("says a milestone nothing hangs off has no workstreams", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation, launch])));

    const { container } = open("milestone-launch");
    await act(async () => {});

    expect(screen.getByText("No workstreams yet.")).toBeInTheDocument();
    expect(container.querySelectorAll(".workstream-card")).toHaveLength(0);
  });

  /** Reachable two ways: the URL is guessable, and `delete_milestone` can take
   *  this goal out of the plan while its page is open and polling. */
  it("says so when the plan holds no such milestone", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-gone");
    await act(async () => {});

    expect(screen.queryByText("Loading milestone…")).toBeNull();
    expect(
      screen.getByText("This project’s plan has no milestone milestone-gone."),
    ).toBeInTheDocument();
  });

  it("follows the plan as it is written", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([launch]))
      .mockImplementation(plan([foundation, launch]));
    vi.stubGlobal("fetch", fetch);

    open();
    await act(async () => {});
    expect(screen.queryByRole("article", { name: "Data model" })).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByRole("article", { name: "Data model" })).toBeInTheDocument();
  });

  it("reports a failure that leaves it with nothing to show", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(unavailable()));

    open();
    await act(async () => {});

    expect(
      screen.getByText("Could not load milestone: store unavailable"),
    ).toBeInTheDocument();
  });

  it("says when it has stopped following the plan, keeping the last one on screen", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementation(unavailable());
    vi.stubGlobal("fetch", fetch);

    open();
    await act(async () => {});

    await act(async () => vi.advanceTimersByTimeAsync(3000));

    expect(screen.getByText("Not updating: store unavailable")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Data model" })).toBeInTheDocument();
  });
});
