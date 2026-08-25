import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiMilestone, ApiProject } from "./api";
import { ProjectMilestonesPage } from "./project-milestones";

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

  it("draws the timeline first, then a card per milestone in the same order", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([launch, foundation])));

    const { container } = render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    const timeline = screen.getByRole("region", { name: "Milestone timeline" });
    expect(timeline.querySelector(".milestone-map")).not.toBeNull();

    const cards = [...container.querySelectorAll<HTMLElement>(".milestone-card")];
    // A dependency is drawn before the goal that needs it, and the cards read
    // in that same order rather than in the store's.
    expect(cards.map((card) => card.querySelector("h2")?.textContent)).toEqual([
      "Foundation",
      "Launch",
    ]);
    expect(timeline.compareDocumentPosition(cards[0])).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("gives each card the detail a timeline node has no room for", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([launch, foundation])));

    render(<ProjectMilestonesPage projectId="project-1" />);
    await act(async () => {});

    const planned = screen.getByRole("article", { name: "Foundation" });
    expect(within(planned).getByText("Build the shared project model.")).toBeInTheDocument();
    expect(within(planned).getByText("milestone-foundation")).toBeInTheDocument();
    expect(within(planned).getByText("2 workstreams")).toBeInTheDocument();
    const workstreams = within(planned).getByRole("list", {
      name: "Workstreams for Foundation",
    });
    expect(
      within(workstreams).getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["Data modelPersist the plan.", "Timeline view"]);

    const shipping = screen.getByRole("article", { name: "Launch" });
    // The dependency reads as the goal it names, not as the id recorded.
    expect(within(shipping).getByText("Depends on Foundation")).toBeInTheDocument();
    expect(within(shipping).getByText("0 workstreams")).toBeInTheDocument();
    expect(within(shipping).getByText("No workstreams yet.")).toBeInTheDocument();
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
    expect(screen.queryByRole("article", { name: "Launch" })).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByRole("article", { name: "Launch" })).toBeInTheDocument();
    expect(screen.queryByText("Loading milestones…")).toBeNull();
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
    expect(screen.getByRole("article", { name: "Foundation" })).toBeInTheDocument();
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
