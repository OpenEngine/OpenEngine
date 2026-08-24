import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MilestoneVisualizer } from "./milestones";

describe("MilestoneVisualizer", () => {
  it("renders the placeholder milestones on one timeline", () => {
    render(<MilestoneVisualizer />);

    const timeline = screen.getByRole("region", { name: "Milestone timeline" });
    const milestones = within(timeline).getAllByRole("listitem");

    expect(milestones).toHaveLength(4);
    expect(milestones.map((milestone) => milestone.textContent)).toEqual([
      "01Milestone 1",
      "02Milestone 2",
      "03Milestone 3",
      "04Milestone 4",
    ]);
  });
});
