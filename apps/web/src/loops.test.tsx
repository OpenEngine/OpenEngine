import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { LoopPage, NewLoopPage } from "./loops";
import { Sidebar } from "./sidebar";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  getLoopSettings: vi.fn(),
  setLoopSettings: vi.fn(),
  listLoops: vi.fn(),
  getLoop: vi.fn(),
  getLoopDefaults: vi.fn(),
  createLoop: vi.fn(),
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
    vi.mocked(api.listLoops).mockResolvedValue([]);
  });

  it("offers a new loop and lists the loops there are", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([loop]);
    render(<Sidebar runs={[]} runners={["codex"]} initialSection="loops" />);

    expect(screen.getByRole("link", { name: "+ New Loop" })).toHaveAttribute("href", "/loops/new");
    expect(await screen.findByRole("link", { name: /Dead code/ }))
      .toHaveAttribute("href", "/loops/loop-1");
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

const loop: api.Loop = {
  loopId: "loop-1",
  name: "Dead code",
  repository: "/repo",
  prompt: "Find and delete unused code.",
  everyMinutes: 30,
  activeHours: { start: "09:00", end: "17:00" },
  maxWorkOrders: 2,
  maxDailySpend: 5,
  createdAt: "2026-10-01T12:00:00-06:00",
  running: false,
  nextRunAt: "2099-01-01T09:00:00Z",
  deferredUntil: null,
  spentToday: 1.25,
  workOrders: [{ runId: "run-1", name: "Delete unused helpers", phase: "running_agent" }],
};

const config = {
  agents: [],
  runners: [],
  defaultAgent: "",
  defaultRunner: "",
  repositories: [{ name: "repo", path: "/repo" }],
  workflows: [],
} as unknown as api.EngineConfig;

describe("New loop form", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getLoopDefaults).mockResolvedValue({
      everyMinutes: 60,
      activeHours: { start: "08:00", end: "18:00" },
      maxWorkOrders: 4,
      maxDailySpend: 7.5,
    });
    vi.mocked(api.createLoop).mockResolvedValue(loop);
  });

  it("starts from the exit criteria and creates the loop with its prompt", async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    render(<NewLoopPage config={config} />);
    expect(screen.getByRole("heading", { name: "Create a Loop" })).toBeVisible();
    expect(await screen.findByLabelText("Max WorkOrders per day")).toHaveValue(4);

    await user.type(screen.getByLabelText("Name"), "Dead code");
    await user.type(screen.getByLabelText("Loop prompt"), "Find and delete unused code.");
    await user.click(screen.getByRole("button", { name: "Create Loop" }));

    await waitFor(() => expect(api.createLoop).toHaveBeenCalledWith({
      name: "Dead code",
      repository: "/repo",
      prompt: "Find and delete unused code.",
      everyMinutes: 60,
      activeHours: { start: "08:00", end: "18:00" },
      maxWorkOrders: 4,
      maxDailySpend: 7.5,
    }));
    expect(assign).toHaveBeenCalledWith("/loops/loop-1");
    vi.unstubAllGlobals();
  });

  it("keeps a field edited before the defaults arrive", async () => {
    let resolve!: (defaults: Awaited<ReturnType<typeof api.getLoopDefaults>>) => void;
    vi.mocked(api.getLoopDefaults).mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<NewLoopPage config={config} />);
    const most = screen.getByLabelText("Max WorkOrders per day");
    fireEvent.change(most, { target: { value: "9" } });
    resolve({ everyMinutes: 60, activeHours: { start: "08:00", end: "18:00" },
      maxWorkOrders: 4, maxDailySpend: 7.5 });
    await waitFor(() => expect(screen.getByLabelText("Max spend ($/day)")).toHaveValue(7.5));
    expect(most).toHaveValue(9);
  });
});

describe("Loop page", () => {
  it("shows when it runs next, what it spent today and its WorkOrders", async () => {
    vi.mocked(api.getLoop).mockResolvedValue(loop);
    render(<LoopPage loopId="loop-1" />);

    expect(await screen.findByRole("heading", { name: "Dead code" })).toBeVisible();
    expect(screen.getByText(new Date("2099-01-01T09:00:00Z").toLocaleString())).toBeVisible();
    expect(screen.getByText("$1.25 of $5.00")).toBeVisible();
    expect(screen.getByRole("link", { name: "Delete unused helpers" }))
      .toHaveAttribute("href", "/runs/run-1");
  });

  it("says a deferred loop waits for the WorkOrder it deferred to", async () => {
    vi.mocked(api.getLoop).mockResolvedValue({ ...loop, deferredUntil: "run-9" });
    render(<LoopPage loopId="loop-1" />);
    expect(await screen.findByText("After WorkOrder run-9 completes")).toBeVisible();
  });
});
