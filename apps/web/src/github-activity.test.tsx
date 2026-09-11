import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiGithubActivity, ApiGithubComment } from "./api";
import {
  GITHUB_COMMENTS_ANCHOR,
  GithubActivityPanel,
  useRunGithubComments,
} from "./github-activity";

/** The panel as the WorkOrder page wears it: the hook that fetches, feeding
 *  the panel that draws. Kept together here because the thing worth testing
 *  is what a reader ends up seeing for a given answer from the engine. */
function Panel({ runId = "run-1" }: { runId?: string }) {
  return <GithubActivityPanel {...useRunGithubComments(runId)} />;
}

function comment(fields: Partial<ApiGithubComment> = {}): ApiGithubComment {
  return {
    commentId: "1",
    event: "issue_comment",
    repository: "acme/api",
    number: 7,
    author: "someone",
    url: "https://github.com/acme/api/pull/7#issuecomment-1",
    excerpt: "please address the review",
    status: "replied",
    detail: "",
    runId: "run-1",
    dispatchedRunId: "run-1",
    runUrl: "/runs/run-1",
    startedRun: false,
    reply: "Forwarded to work order `run-1`.",
    seenAt: 1_757_000_000,
    startedAt: 1_757_000_001,
    dispatchedAt: 1_757_000_030,
    repliedAt: 1_757_000_031,
    ...fields,
  };
}

function activity(fields: Partial<ApiGithubActivity> = {}): ApiGithubActivity {
  return {
    repository: "acme/api",
    configured: true,
    queued: 0,
    working: false,
    sessions: 0,
    comments: [],
    ...fields,
  };
}

/** One answer for the activity endpoint, and a record of what was asked. */
function server(answer: () => Promise<Response>) {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/github-comments")) return answer();
    throw new Error(`unexpected request to ${path}`);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("tells the whole of what happened to a comment", async () => {
  server(async () => json(activity({ comments: [comment()] })));
  render(<Panel />);

  const row = await screen.findByRole("listitem");
  expect(within(row).getByText("Replied")).toBeInTheDocument();
  expect(row.textContent).toContain("someone");
  expect(row.textContent).toContain("acme/api#7");
  expect(row.textContent).toContain("please address the review");
  // Received, picked up, forwarded, replied: the steps, in the order they
  // happened, each with the time it happened at.
  const steps = within(row).getByText(/Received/).textContent ?? "";
  expect(steps).toMatch(/Received .* · Picked up .* · Forwarded to run-1 .* · Replied /);
  expect(row.textContent).toContain("Forwarded to work order `run-1`.");
  expect(within(row).getByRole("link", { name: /View comment/ })).toHaveAttribute(
    "href",
    "https://github.com/acme/api/pull/7#issuecomment-1",
  );
  expect(within(row).getByRole("link", { name: /Open WorkOrder run-1/ })).toHaveAttribute(
    "href",
    "/runs/run-1",
  );
});

it("asks only for the WorkOrder it was given", async () => {
  const fetcher = server(async () => json(activity()));
  render(<Panel />);

  await waitFor(() => expect(fetcher).toHaveBeenCalled());
  expect(String(fetcher.mock.calls[0][0])).toBe("/api/runs/run-1/github-comments");
});

it("is anchored where the WorkOrder page's link points", async () => {
  server(async () => json(activity({ comments: [comment()] })));
  const { container } = render(<Panel />);

  await screen.findByRole("listitem");
  expect(container.querySelector(`#${GITHUB_COMMENTS_ANCHOR}`)).not.toBeNull();
});

it("says why a comment was ignored rather than leaving it out", async () => {
  server(async () =>
    json(
      activity({
        comments: [
          comment({
            status: "ignored",
            detail: "stranger cannot write to acme/api",
            reply: "",
            dispatchedRunId: "",
            runUrl: "",
            dispatchedAt: 0,
            repliedAt: 0,
          }),
        ],
      }),
    ),
  );
  render(<Panel />);

  const row = await screen.findByRole("listitem");
  expect(within(row).getByText("Ignored")).toBeInTheDocument();
  expect(row.textContent).toContain("stranger cannot write to acme/api");
  // Nothing was forwarded, so there is no WorkOrder to offer and no step
  // claiming one was reached.
  expect(within(row).queryByRole("link", { name: /Open WorkOrder/ })).toBeNull();
  expect(within(row).getByText(/Received/).textContent).not.toContain("Forwarded");
});

it("distinguishes a comment that started work from one that steered it", async () => {
  server(async () => json(activity({ comments: [comment({ startedRun: true })] })));
  render(<Panel />);

  const row = await screen.findByRole("listitem");
  const steps = within(row).getByText(/Received/).textContent ?? "";
  expect(steps).toContain("Started run-1");
  expect(steps).not.toContain("Forwarded to");
});

it("says what the concierge is doing right now", async () => {
  server(async () =>
    json(
      activity({
        queued: 2,
        working: true,
        sessions: 3,
        comments: [comment({ status: "working", reply: "", repliedAt: 0, dispatchedAt: 0 })],
      }),
    ),
  );
  render(<Panel />);

  expect(await screen.findByText("Answering a comment")).toBeInTheDocument();
  expect(screen.getByText("2 queued")).toBeInTheDocument();
  expect(screen.getByText("3 open sessions")).toBeInTheDocument();
  expect(
    screen.getByText("1 comment still moving through the engine."),
  ).toBeInTheDocument();
});

it("says a webhook has delivered nothing yet rather than looking broken", async () => {
  server(async () => json(activity()));
  render(<Panel />);

  expect(
    await screen.findByText(
      "No GitHub comments have been delivered for this WorkOrder\u2019s pull request.",
    ),
  ).toBeInTheDocument();
});

it("draws nothing where no webhook is configured", async () => {
  const fetcher = server(async () => json(activity({ repository: "", configured: false })));
  const { container } = render(<Panel />);

  await waitFor(() => expect(fetcher).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});

it("keeps the rows on screen when a later poll fails", async () => {
  let answered = false;
  server(async () => {
    if (answered) return json({ error: "engine is restarting" }, 503);
    answered = true;
    return json(activity({ comments: [comment()] }));
  });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    render(<Panel />);
    await screen.findByRole("listitem");
    await vi.advanceTimersByTimeAsync(4000);
    expect(await screen.findByText(/engine is restarting/)).toBeInTheDocument();
    // The comment is still true; only the reading of what is happening now is
    // missing, so the row stays.
    expect(screen.getByRole("listitem")).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});
