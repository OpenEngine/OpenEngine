import { describe, expect, it } from "vitest";

import { routeForPath } from "./routes";

describe("routeForPath", () => {
  it("routes an encoded milestone deep link to its details page", () => {
    expect(
      routeForPath(
        "/projects/project%20with%20spaces/milestones/milestone%2Fencoded/",
      ),
    ).toEqual({
      kind: "milestone",
      projectId: "project with spaces",
      milestoneId: "milestone/encoded",
    });
  });

  it("keeps the parent milestones route distinct", () => {
    expect(routeForPath("/projects/project-1/milestones")).toEqual({
      kind: "project",
      projectId: "project-1",
    });
  });

  it("routes the milestone task form separately from milestone details", () => {
    expect(
      routeForPath("/projects/project-1/milestones/milestone-1/tasks/new"),
    ).toEqual({
      kind: "new-task",
      projectId: "project-1",
      milestoneId: "milestone-1",
    });
  });

  it("routes a milestone scoping chat separately from milestone details", () => {
    expect(
      routeForPath("/projects/project-1/milestones/milestone-1/scope"),
    ).toEqual({
      kind: "milestone-scope",
      projectId: "project-1",
      milestoneId: "milestone-1",
    });
  });

  /** A page of its own rather than a conversation: everything unrecognised
   *  falls through to chat, so this has to be matched before it. */
  it("routes the rail's graph icon to the utilization page", () => {
    expect(routeForPath("/utilization")).toEqual({ kind: "utilization" });
    expect(routeForPath("/utilization/")).toEqual({ kind: "utilization" });
  });
});
