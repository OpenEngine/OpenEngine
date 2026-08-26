import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MilestoneTimeline,
  MilestoneTimelineVisual,
  mapMinHeight,
  orderMilestones,
} from "./milestone-timeline";
import type { ApiMilestone, ApiProject } from "./api";

const foundation: ApiMilestone = {
  milestoneId: "foundation",
  name: "Foundation",
  description: "Build the shared project model.",
  dependencies: [],
  workstreams: [
    { workstreamId: "workstream-data", name: "Data model", scope: "Persist the plan." },
  ],
};
// The same milestone once the planner has hung more work off it, kept apart so
// the single-workstream case above stays the plain one.
const staffed: ApiMilestone = {
  ...foundation,
  workstreams: [
    ...foundation.workstreams,
    { workstreamId: "workstream-web", name: "Timeline view", scope: "Draw the plan." },
    { workstreamId: "workstream-tools", name: "Planner tools", scope: "Record the plan." },
  ],
};
const launch: ApiMilestone = {
  milestoneId: "launch",
  name: "Launch",
  description: "Ship the project to users.",
  dependencies: ["foundation"],
  workstreams: [],
};
const project: ApiProject = {
  projectId: "project-1",
  name: "Engine roadmap",
  archived: false,
};
// A space is not something the store puts in an id, but it is what pins the
// path this component asks for to the one `getProjectMilestones` builds.
const other: ApiProject = {
  projectId: "project 2",
  name: "Second plan",
  archived: false,
};

function milestonesUrl(id: string) {
  return `/api/projects/${encodeURIComponent(id)}/milestones`;
}

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

describe("milestone timeline", () => {
  it("places dependencies before the milestones that need them", () => {
    expect(orderMilestones([launch, foundation])).toEqual([foundation, launch]);
  });

  it("shows milestone names, descriptions, and dependency lines", () => {
    const { container } = render(
      <MilestoneTimelineVisual milestones={[launch, foundation]} projectId={project.projectId} />,
    );

    expect(screen.getByText("Foundation")).toBeInTheDocument();
    expect(screen.getByText("Launch")).toBeInTheDocument();
    expect(
      screen.getByRole("tooltip", { name: "Build the shared project model." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tooltip", { name: "Ship the project to users." })).toBeInTheDocument();
    expect(container.querySelector('[data-from="foundation"][data-to="launch"]')).not.toBeNull();
  });

  it("anchors the earliest milestone to the left of the viewport", () => {
    const later = Array.from({ length: 4 }, (_, index) => ({
      ...launch,
      milestoneId: `later-${index}`,
      dependencies: index === 0 ? [foundation.milestoneId] : [`later-${index - 1}`],
    }));
    const { container } = render(
      <MilestoneTimelineVisual milestones={[foundation, ...later]} projectId={project.projectId} />,
    );
    const map = container.querySelector<HTMLElement>(".milestone-map")!;
    const nodes = container.querySelectorAll<HTMLElement>(".milestone-node");
    const width = Number.parseFloat(map.style.minWidth);

    expect(nodes[0].style.left).toBe("152px");
    expect(nodes[1].style.left).toBe("362px");
    expect(width - Number.parseFloat(nodes[nodes.length - 1].style.left)).toBeGreaterThanOrEqual(
      140,
    );
  });

  it("lists each milestone's workstreams beneath it, in the order the API sent", () => {
    render(
      <MilestoneTimelineVisual milestones={[staffed, launch]} projectId={project.projectId} />,
    );

    const list = screen.getByRole("list", { name: "Foundation" });
    const items = screen.getAllByRole("listitem");

    expect(list).toContainElement(items[0]);
    // Newest first is decided by the store (`ORDER BY sequence DESC`), so the
    // component may not re-sort what it was handed.
    expect(items.map((item) => item.querySelector("span")?.textContent)).toEqual([
      "Data model",
      "Timeline view",
      "Planner tools",
    ]);
    // A milestone with no workstreams gets no empty list under its name.
    expect(screen.queryByRole("list", { name: "Launch" })).toBeNull();
  });

  it("gives a workstream's scope the tooltip the description already uses", () => {
    render(<MilestoneTimelineVisual milestones={[foundation]} projectId={project.projectId} />);

    const item = screen.getByRole("listitem");
    const scope = screen.getByRole("tooltip", { name: "Persist the plan." });

    // Reachable by keyboard, not only by a hover a touch device cannot make --
    // and by the link's own tab stop rather than a second one on the bullet
    // carrying it, which would stop twice on the one workstream.
    expect(item).not.toHaveAttribute("tabindex");
    expect(screen.getByRole("link", { name: "Data model" })).toHaveAccessibleDescription(
      "Persist the plan.",
    );
    expect(item).toContainElement(scope);
    expect(item).not.toHaveAttribute("title");
  });

  it("opens the milestone page from any workstream hanging off it", () => {
    render(
      <MilestoneTimelineVisual milestones={[staffed, launch]} projectId={other.projectId} />,
    );

    // Every bullet under one goal leads to that goal's page: the page is where
    // the workstreams are told apart, so the milestone is what a click needs.
    expect(
      screen.getAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")]),
    ).toEqual([
      ["Data model", "/projects/project%202/milestones/foundation"],
      ["Timeline view", "/projects/project%202/milestones/foundation"],
      ["Planner tools", "/projects/project%202/milestones/foundation"],
    ]);
  });

  it("hangs a tooltip off the window, above what it describes", async () => {
    const user = userEvent.setup();
    render(<MilestoneTimelineVisual milestones={[foundation]} projectId={project.projectId} />);
    const item = screen.getByRole("listitem");
    const scope = screen.getByRole("tooltip", { name: "Persist the plan." });
    // jsdom lays nothing out, so the bullet is handed the box a browser would
    // give the last one in a list the map has scrolled.
    item.getBoundingClientRect = () => ({ top: 600, left: 300, width: 170 }) as DOMRect;

    await user.hover(item);

    // Measured from the window's edges, not the map's: the map is the box that
    // was clipping the tooltip and scrolling it out of sight.
    expect(scope.style.getPropertyValue("--tooltip-bottom")).toBe(
      `${window.innerHeight - 600 + 8}px`,
    );
    expect(scope.style.getPropertyValue("--tooltip-left")).toBe("385px");
  });

  it("keeps a tooltip on a milestone at the window's edge inside it", async () => {
    const user = userEvent.setup();
    render(<MilestoneTimelineVisual milestones={[foundation]} projectId={project.projectId} />);
    const node = screen.getByText("Foundation").closest(".milestone-node") as HTMLElement;
    const tooltip = screen.getByRole("tooltip", { name: "Build the shared project model." });
    // Scrolled far enough right that the node itself hangs off the window.
    node.getBoundingClientRect = () => ({ top: 600, left: 1000, width: 170 }) as DOMRect;

    await user.hover(node);

    // Half of a tooltip is measured, which jsdom reports as 0; what the clamp
    // has to hold on its own is the gutter.
    expect(tooltip.style.getPropertyValue("--tooltip-left")).toBe(
      `${window.innerWidth - 12}px`,
    );
  });

  it("grows the map so the deepest node's bullets stay inside it", () => {
    const { container, rerender } = render(
      <MilestoneTimelineVisual milestones={[staffed]} projectId={project.projectId} />,
    );
    const height = () =>
      Number.parseFloat(container.querySelector<HTMLElement>(".milestone-map")!.style.minHeight);

    // The node is out of flow, so nothing but this reserves room for it: top
    // 83 + dot/name 66 + grid gap 9 + three 15px bullets + two 4px gaps.
    expect(mapMinHeight([staffed])).toBe(height());
    expect(height()).toBeGreaterThanOrEqual(83 + 66 + 9 + 3 * 15 + 2 * 4);

    // A plan without workstreams is left on the floor the map already had.
    rerender(<MilestoneTimelineVisual milestones={[launch]} projectId={project.projectId} />);
    expect(height()).toBe(180);
  });

  it("renders an empty state without inventing milestones", () => {
    render(<MilestoneTimelineVisual milestones={[]} projectId={project.projectId} />);

    expect(
      screen.getByText("No milestones have been added to this project yet."),
    ).toBeInTheDocument();
  });

});

describe("MilestoneTimeline", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("expands a new project's collapsed timeline when its first milestone appears", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([foundation])));
    const { rerender } = render(<MilestoneTimeline collapsedUntilMilestone />);
    const timeline = screen.getByRole("region", { name: "Milestone timeline" });

    expect(timeline).toHaveClass("milestone-timeline-collapsed");

    rerender(<MilestoneTimeline project={project} collapsedUntilMilestone />);
    await act(async () => {});

    expect(timeline).toHaveClass("milestone-timeline-expanded");
  });

  it("opens an existing project's timeline by default", () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(plan([])));

    render(<MilestoneTimeline project={project} />);

    expect(screen.getByRole("region", { name: "Milestone timeline" })).toHaveClass(
      "milestone-timeline-expanded",
    );
  });

  it("follows milestones added and removed while the page stays open", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementationOnce(plan([foundation, launch]))
      .mockImplementation(plan([launch]));
    vi.stubGlobal("fetch", fetch);

    render(<MilestoneTimeline project={project} />);
    await act(async () => {});
    expect(screen.getByText("Foundation")).toBeInTheDocument();
    expect(screen.queryByText("Launch")).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByText("Launch")).toBeInTheDocument();
    // Refreshed in place: a poll does not send the page back to its first load.
    expect(screen.queryByText("Loading milestones…")).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.queryByText("Foundation")).toBeNull();
    expect(screen.getByText("Launch")).toBeInTheDocument();
  });

  it("follows a workstream hung off a milestone that did not otherwise change", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementation(plan([staffed]));
    vi.stubGlobal("fetch", fetch);

    render(<MilestoneTimeline project={project} />);
    await act(async () => {});

    expect(screen.getByText("Data model")).toBeInTheDocument();
    expect(screen.queryByText("Timeline view")).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    // The milestone itself is untouched, so only the workstreams tell the two
    // polls apart -- which is what `sameMilestones` has to notice.
    expect(screen.getByText("Timeline view")).toBeInTheDocument();
  });

  it("holds the last good timeline through a failed poll", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementationOnce(unavailable())
      .mockImplementation(plan([foundation, launch]));
    vi.stubGlobal("fetch", fetch);

    render(<MilestoneTimeline project={project} />);
    await act(async () => {});

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByText("Foundation")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load milestones/)).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.getByText("Launch")).toBeInTheDocument();
  });

  it("reports a failure that leaves it with nothing to show", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(unavailable()));

    render(<MilestoneTimeline project={project} />);
    await act(async () => {});

    expect(
      screen.getByText("Could not load milestones: store unavailable"),
    ).toBeInTheDocument();
  });

  it("says so when it has stopped following the plan, and stops saying so", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockImplementationOnce(plan([foundation]))
      .mockImplementationOnce(unavailable())
      .mockImplementationOnce(unavailable())
      .mockImplementationOnce(unavailable())
      .mockImplementation(plan([foundation, launch]));
    vi.stubGlobal("fetch", fetch);

    render(<MilestoneTimeline project={project} />);
    await act(async () => {});

    await act(async () => vi.advanceTimersByTimeAsync(1000));
    // One failure is a blip, and the timeline is still believed to be current.
    expect(screen.queryByText(/Not updating/)).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(2000));

    // A run of them is an outage: the last known plan stays on screen, but it
    // no longer claims to be following anything.
    expect(screen.getByText("Not updating: store unavailable")).toBeInTheDocument();
    expect(screen.getByText("Foundation")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(screen.queryByText(/Not updating/)).toBeNull();
    expect(screen.getByText("Launch")).toBeInTheDocument();
  });

  it("polls the project it was given, and follows a switch to another", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(async (url: string) =>
      url === milestonesUrl(other.projectId)
        ? await plan([launch])()
        : await plan([foundation])(),
    );
    vi.stubGlobal("fetch", fetch);

    const { rerender } = render(<MilestoneTimeline project={project} />);
    await act(async () => {});

    expect(fetch.mock.lastCall?.[0]).toBe("/api/projects/project-1/milestones");
    expect(screen.getByText("Foundation")).toBeInTheDocument();

    rerender(<MilestoneTimeline project={other} />);

    // The first project's plan is not left standing under the second project's
    // name while the new one is being read.
    expect(screen.queryByText("Foundation")).toBeNull();
    expect(screen.getByText("Loading milestones…")).toBeInTheDocument();

    await act(async () => {});

    expect(fetch.mock.lastCall?.[0]).toBe("/api/projects/project%202/milestones");
    expect(screen.getByText("Launch")).toBeInTheDocument();
    expect(screen.queryByText("Foundation")).toBeNull();
  });

  it("stops polling once it leaves the page", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(plan([foundation]));
    vi.stubGlobal("fetch", fetch);

    const { unmount } = render(<MilestoneTimeline project={project} />);
    await act(async () => {});
    const answered = fetch.mock.calls.length;

    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(5000));

    expect(fetch).toHaveBeenCalledTimes(answered);
  });

  it("stops polling when it leaves the page mid-request", async () => {
    vi.useFakeTimers();
    let answer = () => {};
    const held = new Promise<void>((resolve) => {
      answer = resolve;
    });
    const fetch = vi.fn().mockImplementation(async () => {
      await held;
      return plan([foundation])();
    });
    vi.stubGlobal("fetch", fetch);

    const { unmount } = render(<MilestoneTimeline project={project} />);
    expect(fetch).toHaveBeenCalledTimes(1);

    // Unmounted with the first request still open, so there is no timer left to
    // clear: only the abort keeps the answer from scheduling the next poll.
    unmount();
    await act(async () => {
      answer();
    });
    await act(async () => vi.advanceTimersByTimeAsync(5000));

    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
