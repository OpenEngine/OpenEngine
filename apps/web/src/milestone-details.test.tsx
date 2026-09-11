import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject, ApiWorkflowRun } from "./api";
import { MilestoneDetailsPage } from "./milestone-details";

const foundation: ApiMilestone = {
  milestoneId: "milestone-foundation",
  name: "Foundation",
  description: "Build the shared project model.",
  dependencies: [],
};
const launch: ApiMilestone = {
  milestoneId: "milestone-launch",
  name: "Launch",
  description: "Ship the project to users.",
  dependencies: ["milestone-foundation"],
};
const project: ApiProject = {
  projectId: "project-1",
  name: "Engine roadmap",
  archived: false,
  conversationUrl: "/conversations/agi-1",
};

/** A run is the record of one task, and carries the milestone it was started
 *  under -- which is all this page groups them by. */
function run(
  runId: string,
  name: string,
  milestoneId: string | null,
  phase = "succeeded",
): ApiWorkflowRun {
  return {
    runId,
    name,
    workflowId: "delivery",
    workflowName: "Delivery",
    taskId: `task-${runId}`,
    milestoneId,
    taskPrompt: name,
    repository: ".",
    repositoryContext: { repository: "." },
    phase,
    terminalOutcome: null,
    failureReason: "",
  };
}

const persisting = run("run-1", "Persist milestones", "milestone-foundation", "running_agent");
const migrating = run("run-2", "Add the milestone table", "milestone-foundation");
const unplanned = run("run-3", "A chore nobody planned", null);
const shipping = run("run-4", "Cut the release", "milestone-launch");
const reviewing = run(
  "run-5",
  "Approve the release",
  "milestone-foundation",
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
  runs = [persisting, migrating],
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
  it("starts scheduled work and reports failures without removing the Start action", async () => {
    const scheduled = run("scheduled", "Planned work", foundation.milestoneId, "scheduled");
    const fetcher = vi.fn(plan([foundation]));
    vi.stubGlobal("fetch", fetcher);
    open(foundation.milestoneId, [scheduled]);
    const button = await screen.findByRole("button", { name: "Start Planned work" });
    expect(screen.queryByRole("link", { name: /Planned work/ })).not.toBeInTheDocument();
    fetcher.mockImplementationOnce(unavailable());
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not start workorder");
    fetcher.mockImplementationOnce(async () => new Response(JSON.stringify({ ...scheduled, phase: "running_agent" })));
    fireEvent.click(screen.getByRole("button", { name: "Start Planned work" }));
    expect(await screen.findByRole("link", { name: /Planned work/ })).toHaveAttribute("href", "/runs/scheduled");
    expect(fetcher).toHaveBeenCalledWith("/api/runs/scheduled/start", expect.objectContaining({ method: "POST" }));
    expect(screen.queryByRole("button", { name: "Start Planned work" })).not.toBeInTheDocument();
  });

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
    const scope = screen.getByRole("link", { name: "Scope" });
    expect(scope).toHaveAttribute(
      "href",
      "/projects/project-1/milestones/milestone-launch/scope",
    );
  });

  it("lists every task started under the milestone, and leads to its run", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation, launch])));

    open("milestone-foundation", [persisting, migrating, unplanned, shipping]);
    await act(async () => {});

    const tasks = screen.getByRole("list", { name: "Tasks in Foundation" });
    // A task under no milestone, and one under another goal, are not this
    // milestone's work.
    expect(within(tasks).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "/runs/run-1",
      "/runs/run-2",
    ]);
    expect(within(tasks).getByText("Persist milestones")).toBeInTheDocument();
    // The stage the run has reached, in the workflow's own words while it runs.
    expect(within(tasks).getByText("running agent")).toBeInTheDocument();
    expect(within(tasks).getByText("succeeded")).toBeInTheDocument();
    // A non-terminal run is work left under this heading.
    expect(screen.getByText("Tasks").nextSibling).toHaveTextContent("2");
    expect(screen.getByText("Unfinished").nextSibling).toHaveTextContent("1");
  });

  it("counts a task awaiting human review as unfinished and calls it out", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [reviewing]);
    await act(async () => {});

    expect(screen.getByText("Unfinished").nextSibling).toHaveTextContent("1");
    expect(screen.getByText("Awaiting review").nextSibling).toHaveTextContent("1");
  });

  it("says a milestone nothing has been started under has nothing in it", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", []);
    await act(async () => {});

    expect(
      screen.getByText("No tasks have been started under this milestone yet."),
    ).toBeInTheDocument();
    expect(screen.getByText("Tasks").nextSibling).toHaveTextContent("0");
  });

  it("does not claim there are no tasks before the runs poll answers", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));

    open("milestone-foundation", [], { runsLoaded: false });
    await act(async () => {});

    expect(screen.getByText("Loading tasks…")).toBeInTheDocument();
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

    expect(screen.getByText("Could not load tasks: runs unavailable")).toBeInTheDocument();
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
    expect(screen.queryByRole("heading", { name: "Foundation", level: 1 })).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByRole("heading", { name: "Foundation", level: 1 })).toBeInTheDocument();
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
    expect(screen.getByRole("heading", { name: "Foundation", level: 1 })).toBeInTheDocument();
  });
});
