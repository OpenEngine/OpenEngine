import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiRunnerUtilization } from "./api";
import { UtilizationPage } from "./utilization";

const cached: ApiRunnerUtilization = {
  runner: "claude",
  plan: "max",
  error: "",
  readAt: 1_757_000_000,
  windows: [
    { windowId: "five_hour", label: "5-hour", usedPercent: 4, resetsAt: "" },
    { windowId: "seven_day", label: "Weekly", usedPercent: 9, resetsAt: "" },
  ],
};

const scraped: ApiRunnerUtilization = {
  ...cached,
  readAt: 1_757_000_900,
  windows: [
    { windowId: "five_hour", label: "5-hour", usedPercent: 12, resetsAt: "" },
    { windowId: "seven_day", label: "Weekly", usedPercent: 41, resetsAt: "" },
  ],
};

const codex: ApiRunnerUtilization = {
  runner: "codex",
  plan: "prolite",
  error: "",
  readAt: 1_757_000_900,
  windows: [{ windowId: "weekly", label: "Weekly", usedPercent: 7, resetsAt: "" }],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** One answer per endpoint, so the two calls the page makes can be told apart
 *  and released in either order. */
function server(answers: {
  cache?: () => Promise<Response>;
  refresh?: () => Promise<Response>;
}) {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/utilization")
      return (answers.cache ?? (async () => json({ runners: [] })))();
    if (path === "/api/utilization/refresh")
      return (answers.refresh ?? (async () => json({ runners: [] })))();
    throw new Error(`unexpected request to ${path}`);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

function meters(runner: string): string[] {
  const card = screen.getByLabelText(`${runner} utilization`);
  return [...card.querySelectorAll(".usage-window")].map(
    (element) =>
      `${element.querySelector(".usage-window-label")?.textContent} ${element.querySelector(".usage-window-value")?.textContent}`,
  );
}

describe("UtilizationPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("draws the cache first and replaces it with the scrape", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server({
      cache: async () => json({ runners: [cached] }),
      refresh: async () => {
        await held;
        return json({ runners: [scraped, codex] });
      },
    });

    render(<UtilizationPage />);

    // What the page has to show before either provider has answered.
    await waitFor(() => expect(meters("claude")).toEqual(["5-hour 4%", "Weekly 9%"]));

    release!();

    await waitFor(() => expect(meters("claude")).toEqual(["5-hour 12%", "Weekly 41%"]));
    expect(meters("codex")).toEqual(["Weekly 7%"]);
  });

  /** The cache is only worth drawing until the scrape lands. One that answers
   *  late must not put yesterday's figures back on the page. */
  it("never paints a late cache over a scrape that already landed", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server({
      cache: async () => {
        await held;
        return json({ runners: [cached] });
      },
      refresh: async () => json({ runners: [scraped] }),
    });

    render(<UtilizationPage />);

    await waitFor(() => expect(meters("claude")).toEqual(["5-hour 12%", "Weekly 41%"]));

    release!();

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(meters("claude")).toEqual(["5-hour 12%", "Weekly 41%"]);
  });

  it("prints why a runner's figures are not newer, beside the figures", async () => {
    server({
      cache: async () => json({ runners: [] }),
      refresh: async () =>
        json({ runners: [{ ...cached, error: "Codex is not signed in on this machine" }] }),
    });

    render(<UtilizationPage />);

    const card = await screen.findByLabelText("claude utilization");
    expect(within(card).getByText("Codex is not signed in on this machine")).toBeVisible();
    expect(meters("claude")).toEqual(["5-hour 4%", "Weekly 9%"]);
  });

  it("asks every provider again when the reader asks it to", async () => {
    const user = userEvent.setup();
    let scrapes = 0;
    const fetcher = server({
      refresh: async () => {
        scrapes += 1;
        return json({ runners: [scrapes === 1 ? cached : scraped] });
      },
    });

    render(<UtilizationPage />);

    await waitFor(() => expect(meters("claude")).toEqual(["5-hour 4%", "Weekly 9%"]));

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(meters("claude")).toEqual(["5-hour 12%", "Weekly 41%"]));
    // The cache is read once, on open: a refresh already has what it holds.
    expect(
      fetcher.mock.calls.filter(([path]) => String(path) === "/api/utilization"),
    ).toHaveLength(1);
  });

  it("says so when the scrape itself could not be made", async () => {
    server({ refresh: async () => json({ error: "store unavailable" }, 500) });

    render(<UtilizationPage />);

    expect(
      await screen.findByText("Could not read utilization: store unavailable"),
    ).toBeVisible();
  });
});
