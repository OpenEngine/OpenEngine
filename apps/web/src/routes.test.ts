import { describe, expect, it } from "vitest";

import { routeForPath } from "./routes";

describe("routeForPath", () => {
  it("routes the login page", () => {
    expect(routeForPath("/login")).toEqual({ kind: "login" });
    expect(routeForPath("/login/")).toEqual({ kind: "login" });
  });

  it("routes the rail's graph icon to the utilization page", () => {
    expect(routeForPath("/utilization")).toEqual({ kind: "utilization" });
    expect(routeForPath("/utilization/")).toEqual({ kind: "utilization" });
  });

  it("routes the new loop form and a loop's page", () => {
    expect(routeForPath("/loops/new")).toEqual({ kind: "new-loop" });
    expect(routeForPath("/loops/a%20b")).toEqual({ kind: "loop", loopId: "a b" });
  });
});
