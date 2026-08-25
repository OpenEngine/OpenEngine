import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MilestoneTimeline,
  MilestoneTimelineVisual,
  orderMilestones,
} from "./milestone-timeline";
import type { ApiMilestone, ApiProject } from "./api";

const getProjectMilestones = vi.hoisted(() => vi.fn());

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  getProjectMilestones,
}));

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
const project: ApiProject = {
  projectId: "project-1",
  name: "A new project",
  conversationUrl: "/conversations/thread-1",
};

beforeEach(() => getProjectMilestones.mockReset());

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

  it("expands a new project's collapsed timeline when its first milestone appears", async () => {
    getProjectMilestones.mockResolvedValue({ project, milestones: [foundation] });
    const { rerender } = render(<MilestoneTimeline collapsedUntilMilestone />);
    const timeline = screen.getByRole("region", { name: "Milestone timeline" });

    expect(timeline).toHaveClass("milestone-timeline-collapsed");

    rerender(<MilestoneTimeline project={project} collapsedUntilMilestone />);

    await waitFor(() => expect(timeline).toHaveClass("milestone-timeline-expanded"));
  });

  it("opens an existing project's timeline by default", () => {
    getProjectMilestones.mockResolvedValue({ project, milestones: [] });

    render(<MilestoneTimeline project={project} />);

    expect(screen.getByRole("region", { name: "Milestone timeline" })).toHaveClass(
      "milestone-timeline-expanded",
    );
  });
});
