import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatSidebar } from "./chat";
import { EngineRuntimeProvider } from "./runtime";

function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
}

function renderSidebar(onOpenProjectManager?: () => void) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === "/api/threads"
        ? json({ threads: [] })
        : json({ error: "not found" }),
    ),
  );
  return render(
    <EngineRuntimeProvider defaults={{ agentId: "coder", runner: "claude" }}>
      <ChatSidebar onOpenProjectManager={onOpenProjectManager} />
    </EngineRuntimeProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ChatSidebar", () => {
  it("points the next conversation at the project manager", async () => {
    const open = vi.fn();
    const user = userEvent.setup();
    renderSidebar(open);

    await user.click(await screen.findByRole("button", { name: "Project Manager" }));

    expect(open).toHaveBeenCalledOnce();
  });

  it("offers no Projects section when no project manager is configured", async () => {
    renderSidebar();

    expect(await screen.findByRole("button", { name: "+ New chat" })).toBeVisible();
    expect(screen.queryByText("Projects")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Project Manager" })).not.toBeInTheDocument();
  });
});
