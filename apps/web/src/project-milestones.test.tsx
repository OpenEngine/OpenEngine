import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject } from "./api";
import { ProjectMilestonesPage } from "./project-milestones";

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

describe("ProjectMilestonesPage", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reads the project it was routed to, and names the page after it", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(plan([foundation]));
    vi.stubGlobal("fetch", fetch);

    render(<ProjectMilestonesPage projectId="project 1" />);
    expect(screen.getByText("Loading milestones…")).toBeInTheDocument();

    await act(async () => {});

    expect(fetch.mock.lastCall?.[0]).toBe("/api/projects/project%201/milestones");
    expect(screen.getByRole("heading", { name: "Milestones", level: 1 })).toBeInTheDocument();
    // Named from the answer rather than from the projects list, which this page
    // never waits on.
    expect(screen.getByText("Engine roadmap")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Planning conversation" })).toHaveAttribute(
      "href",
      "/conversations/agi-1",
    );
  });

  it("draws the timeline first, then the two work tables under it", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([launch, foundation])));

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    const timeline = screen.getByRole("region", { name: "Milestone timeline" });
    expect(timeline.querySelector(".milestone-map")).not.toBeNull();

    const tables = screen.getAllByRole("table");
    expect(tables.map((table) => table.getAttribute("aria-labelledby"))).toEqual([
      "scheduled-work-title",
      "finished-work-title",
    ]);
    // The graph keeps the top of the page; the tables read below it.
    expect(timeline.compareDocumentPosition(tables[0])).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("gives each work table its columns, and no rows until something fills them", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([launch, foundation])));

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    for (const [title, empty] of [
      ["Scheduled Work", "No scheduled work yet."],
      ["Finished Work", "No finished work yet."],
    ]) {
      expect(screen.getByRole("heading", { name: title, level: 2 })).toBeInTheDocument();
      const table = screen.getByRole("table", { name: title });
      expect(
        within(table).getAllByRole("columnheader").map((cell) => cell.textContent),
      ).toEqual(["Workorder Name", "Workorder Description", "Milestone"]);
      // Nothing is wired to these yet, so the one row says so rather than
      // leaving a headed table that looks like it failed to load.
      expect(within(table).getByText(empty)).toBeInTheDocument();
      expect(within(table).queryAllByRole("link")).toHaveLength(0);
    }
  });

  it("follows the plan as it is written, without redrawing the page", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementation(plan([foundation, launch]));
    vi.stubGlobal("fetch", fetch);

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});
    expect(screen.queryByRole("link", { name: "Launch" })).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getAllByRole("link", { name: "Launch" })).toHaveLength(1);
    expect(screen.queryByText("Loading milestones…")).toBeNull();
  });

  /** Reachable two ways: the URL is guessable, and `delete_milestone` can empty
   *  a plan while this page is open and polling. */
  it("says an emptied plan is empty rather than showing a bare header", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([])));

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    expect(screen.queryByText("Loading milestones…")).toBeNull();
    expect(
      screen.getByText("No milestones have been added to this project yet."),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("link", { name: /milestone/i })).toHaveLength(0);
  });

  it("reports a failure that leaves it with nothing to show", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(unavailable()));

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    expect(
      screen.getByText("Could not load milestones: store unavailable"),
    ).toBeInTheDocument();
  });

  it("says when it has stopped following the plan, keeping the last one on screen", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementation(unavailable());
    vi.stubGlobal("fetch", fetch);

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    await act(async () => vi.advanceTimersByTimeAsync(3000));

    expect(screen.getByText("Not updating: store unavailable")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Foundation" })).toHaveLength(1);
  });

  it("stops polling once it leaves the page", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(plan([foundation]));
    vi.stubGlobal("fetch", fetch);

    const { unmount } = render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});
    const answered = fetch.mock.calls.length;

    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(5000));

    expect(fetch).toHaveBeenCalledTimes(answered);
  });
});
