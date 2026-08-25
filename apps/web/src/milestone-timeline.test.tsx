import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MilestoneTimelineVisual, orderMilestones } from "./milestone-timeline";
import type { ApiMilestone } from "./api";

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

  it("renders an empty state without inventing milestones", () => {
    render(<MilestoneTimelineVisual milestones={[]} />);

    expect(
      screen.getByText("No milestones have been added to this project yet."),
    ).toBeInTheDocument();
  });
});
