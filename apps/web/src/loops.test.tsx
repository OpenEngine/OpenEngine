import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { Sidebar } from "./sidebar";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  getLoopSettings: vi.fn(),
  setLoopSettings: vi.fn(),
}));

const saved: api.LoopSettings = {
  activeHours: { start: "00:00", end: "00:00" },
  maxPrs: 3,
  maxDailySpend: 0,
  runnerStrategy: "least-utilized",
  implementationRunner: "",
  reviewRunner: "",
};

describe("Loops section", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getLoopSettings).mockResolvedValue(saved);
    vi.mocked(api.setLoopSettings).mockImplementation(async (settings) => settings);
  });

  it("reads the settings only once the section is opened", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[]} runners={["codex", "claude"]} />);
    expect(api.getLoopSettings).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Loops" }));

    expect(await screen.findByLabelText("Max PRs")).toHaveValue(3);
    expect(api.getLoopSettings).toHaveBeenCalledOnce();
  });

  it("groups the limits under exit criteria with a tip on what they do", async () => {
    render(<Sidebar runs={[]} runners={["codex"]} initialSection="loops" />);
    const criteria = await screen.findByRole("group", { name: /Exit criteria/ });

    for (const label of ["Active from", "Max PRs", "Max spend ($/day)"]) {
      expect(within(criteria).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(criteria).queryByLabelText("Runner strategy")).not.toBeInTheDocument();
    expect(within(criteria).getByRole("img", {
      name: "Loops stop working if any one of their exit criteria are met.",
    })).toHaveAttribute("data-tip");
  });

  it("offers the implementer and reviewer groups only under manual", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[]} runners={["codex", "claude"]} initialSection="loops" />);
    const strategy = await screen.findByLabelText("Runner strategy");
    expect(screen.queryByLabelText("Implementer group")).not.toBeInTheDocument();

    await user.selectOptions(strategy, "manual");
    await user.selectOptions(screen.getByLabelText("Reviewer group"), "claude");
    await user.clear(screen.getByLabelText("Max PRs"));
    await user.type(screen.getByLabelText("Max PRs"), "5");
    await user.clear(screen.getByLabelText("Max spend ($/day)"));
    await user.type(screen.getByLabelText("Max spend ($/day)"), "20");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.setLoopSettings).toHaveBeenCalledWith({
      ...saved,
      maxPrs: 5,
      maxDailySpend: 20,
      runnerStrategy: "manual",
      implementationRunner: "codex",
      reviewRunner: "claude",
    }));
    expect(await screen.findByText("Saved")).toBeVisible();
  });
});
