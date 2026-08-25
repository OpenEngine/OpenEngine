import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MilestoneTimeline,
  MilestoneTimelineVisual,
  orderMilestones,
} from "./milestone-timeline";
import type { ApiMilestone, ApiProject } from "./api";

const foundation: ApiMilestone = {
  milestoneId: "foundation",
  name: "Foundation",
  description: "Build the shared project model.",
  dependencies: [],
};
const launch: ApiMilestone = {
  milestoneId: "launch",
  name: "Launch",
  description: "Ship the project to users.",
  dependencies: ["foundation"],
};
const project: ApiProject = { projectId: "project-1", name: "Engine roadmap" };

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
      <MilestoneTimelineVisual milestones={[launch, foundation]} />,
    );

    expect(screen.getByText("Foundation")).toBeInTheDocument();
    expect(screen.getByText("Launch")).toBeInTheDocument();
    expect(
      screen.getByRole("tooltip", { name: "Build the shared project model." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tooltip", { name: "Ship the project to users." })).toBeInTheDocument();
    expect(container.querySelector('[data-from="foundation"][data-to="launch"]')).not.toBeNull();
  });

  it("keeps edge tooltips inside the minimum-width timeline", () => {
    const { container } = render(
      <MilestoneTimelineVisual milestones={[foundation, launch]} />,
    );
    const map = container.querySelector<HTMLElement>(".milestone-map")!;
    const nodes = container.querySelectorAll<HTMLElement>(".milestone-node");
    const width = Number.parseFloat(map.style.minWidth);
    const firstCenter = (Number.parseFloat(nodes[0].style.left) / 100) * width;
    const lastCenter = (Number.parseFloat(nodes[1].style.left) / 100) * width;

    expect(firstCenter).toBeGreaterThanOrEqual(140);
    expect(width - lastCenter).toBeGreaterThanOrEqual(140);
  });

  it("renders an empty state without inventing milestones", () => {
    render(<MilestoneTimelineVisual milestones={[]} />);

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
});
