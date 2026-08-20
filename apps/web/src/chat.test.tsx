import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiApproval } from "./api";
import {
  ApprovalEntry,
  errorText,
  outcomeText,
  summaryText,
  toolDetail,
  toolResultText,
} from "./chat";

function approval(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return {
    id: "approval-7",
    status: "pending",
    kind: "command_execution",
    reason: "The command reads repository state.",
    command: "git status",
    cwd: "/repo",
    toolName: null,
    toolCallId: null,
    arguments: null,
    allowedDecisions: ["accept", "accept_for_session", "cancel"],
    decision: null,
    decisionSource: null,
    ...overrides,
  };
}

function questionApproval(overrides: Partial<ApiApproval> = {}): ApiApproval {
  return approval({
    kind: "user_input",
    command: null,
    toolName: "AskUserQuestion",
    allowedDecisions: ["cancel"],
    questions: [{
      id: "api",
      header: "API",
      question: "Which API should remain stable?",
      options: [
        { label: "Public", description: "Preserve the public API" },
        { label: "Internal", description: "Preserve the internal API" },
      ],
      multiSelect: false,
      allowsOther: true,
    }],
    ...overrides,
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("chat display helpers", () => {
  it("keeps status and decision-source precedence for every field combination", () => {
    const statuses = ["pending", "decided", "interrupted"] as const;
    const decisions = [null, "accept", "accept_for_session", "cancel"] as const;
    const sources = [null, "user", "session_grant"] as const;
    const summaries = {
      accept: "Approved · git status",
      accept_for_session: "Approved for this conversation · git status",
      cancel: "Cancelled · git status",
    } as const;
    const outcomes = {
      accept: "Approved.",
      accept_for_session: "Approved, and allowed again for this conversation without asking.",
      cancel: "Cancelled — the action did not run.",
    } as const;

    for (const status of statuses) {
      for (const decision of decisions) {
        for (const decisionSource of sources) {
          const value = approval({ status, decision, decisionSource });
          const expectedSummary =
            status === "pending"
              ? "Approval needed · git status"
              : status === "interrupted"
                ? "Interrupted · git status"
                : decisionSource === "session_grant"
                  ? "Approved automatically · git status"
                  : decision
                    ? summaries[decision]
                    : "Closed · git status";
          expect(summaryText(value)).toBe(expectedSummary);

          if (status === "pending") continue;
          const expectedOutcome =
            status === "interrupted"
              ? "Interrupted — the agent that asked this is gone, so it can no longer be answered."
              : decisionSource === "session_grant"
                ? "Approved automatically: you allowed this exact action for this conversation earlier."
                : decision
                  ? outcomes[decision]
                  : "This request is no longer open.";
          expect(outcomeText(value)).toBe(expectedOutcome);
        }
      }
    }
  });

  it.each([
    [approval(), "Approval needed · git status"],
    [approval({ status: "interrupted" }), "Interrupted · git status"],
    [
      approval({ status: "decided", decision: "cancel", decisionSource: "session_grant" }),
      "Approved automatically · git status",
    ],
    [
      approval({ status: "decided", decision: "accept", decisionSource: "user" }),
      "Approved · git status",
    ],
    [
      approval({
        status: "decided",
        decision: "accept_for_session",
        decisionSource: "user",
      }),
      "Approved for this conversation · git status",
    ],
    [
      approval({ status: "decided", decision: "cancel", decisionSource: "user" }),
      "Cancelled · git status",
    ],
    [approval({ status: "decided" }), "Closed · git status"],
  ])("summarizes approval state", (value, expected) => {
    expect(summaryText(value)).toBe(expected);
  });

  it.each([
    [
      approval({ status: "interrupted", decisionSource: "session_grant" }),
      "Interrupted — the agent that asked this is gone, so it can no longer be answered.",
    ],
    [
      approval({ status: "decided", decision: "cancel", decisionSource: "session_grant" }),
      "Approved automatically: you allowed this exact action for this conversation earlier.",
    ],
    [
      approval({ status: "decided", decision: "accept", decisionSource: "user" }),
      "Approved.",
    ],
    [
      approval({
        status: "decided",
        decision: "accept_for_session",
        decisionSource: "user",
      }),
      "Approved, and allowed again for this conversation without asking.",
    ],
    [
      approval({ status: "decided", decision: "cancel", decisionSource: "user" }),
      "Cancelled — the action did not run.",
    ],
    [approval({ status: "decided" }), "This request is no longer open."],
  ])("describes approval outcomes", (value, expected) => {
    expect(outcomeText(value)).toBe(expected);
  });

  it("formats tool results and picks the first useful detail", () => {
    expect(toolResultText("plain")).toBe("plain");
    expect(toolResultText({ ok: true })).toBe('{\n  "ok": true\n}');
    expect(toolDetail({ query: "first line\nsecond line", url: "https://example.test" })).toBe(
      "first line",
    );
    expect(toolDetail({ command: "x".repeat(80) })).toBe(`${"x".repeat(71)}…`);
    expect(toolDetail(["not", "fields"])).toBe("");
  });

  it("extracts useful errors and provides a fallback", () => {
    expect(errorText("plain failure")).toBe("plain failure");
    expect(errorText(new Error("error failure"))).toBe("error failure");
    expect(errorText({ message: "normalized failure" })).toBe("normalized failure");
    expect(errorText({ code: "unknown" })).toBe("The agent run failed.");
  });
});

describe("ApprovalEntry", () => {
  it("renders only decisions the provider allows", () => {
    render(
      <ApprovalEntry
        threadId="thread-1"
        approval={approval({ allowedDecisions: ["accept", "cancel"] })}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Allow similar actions for this session" }),
    ).not.toBeInTheDocument();
  });

  it("disables every decision as soon as one is submitted", async () => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(new Promise<Response>((done) => (resolve = done))),
    );
    const user = userEvent.setup();
    render(<ApprovalEntry threadId="thread-1" approval={approval()} />);

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Allow similar/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    resolve(
      new Response(JSON.stringify({ approval: approval() }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  it("restores controls and shows the reason when a decision is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "This request was already answered." }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const user = userEvent.setup();
    render(<ApprovalEntry threadId="thread-1" approval={approval()} />);

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("This request was already answered.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("starts pending requests open and folds decided requests", async () => {
    const { rerender } = render(
      <ApprovalEntry threadId="thread-1" approval={approval()} />,
    );
    const details = screen.getByText("Approval needed · git status").closest("details");
    expect(details).toHaveAttribute("open");

    rerender(
      <ApprovalEntry
        threadId="thread-1"
        approval={approval({
          status: "decided",
          decision: "accept",
          decisionSource: "user",
        })}
      />,
    );

    await waitFor(() => expect(details).not.toHaveAttribute("open"));
    expect(screen.getByText("Approved · git status")).toBeInTheDocument();
  });

  it("renders structured questions in a modal and submits a choice", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ approval: { id: "approval-7" } }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    render(
      <ApprovalEntry
        threadId="thread-1"
        approval={questionApproval()}
      />,
    );

    expect(screen.getByRole("dialog", { name: "Agent question" })).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Public/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/threads/thread-1/runs/current/approvals/approval-7",
      expect.objectContaining({ body: '{"answers":{"api":["Public"]}}' }),
    );
  });

  it("cancels a structured question without requiring an answer", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ approval: { id: "approval-7" } }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const user = userEvent.setup();
    const { rerender } = render(
      <ApprovalEntry threadId="thread-1" approval={questionApproval()} />,
    );

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/threads/thread-1/runs/current/approvals/approval-7",
      expect.objectContaining({ body: '{"decision":"cancel"}' }),
    );

    rerender(
      <ApprovalEntry
        threadId="thread-1"
        approval={questionApproval({
          status: "decided",
          decision: "cancel",
          decisionSource: "user",
        })}
      />,
    );
    expect(
      screen.getByText("Cancelled · Which API should remain stable?"),
    ).toBeInTheDocument();
  });
});
