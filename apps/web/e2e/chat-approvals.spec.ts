import { existsSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

/** Written by the agent into its own worktree, so the approval can be checked
 *  by what it produced rather than by what the protocol said about it. */
const COMMAND = "echo approved > allowed.txt";

const SCRIPT: Script = {
  title: "Recording an approval",
  scenarios: [
    {
      steps: [
        { type: "say", text: "Reading the repository first." },
        { type: "run", command: COMMAND },
        { type: "say", text: "Wrote the file." },
      ],
    },
  ],
};

/** The Status cell of the strip under the conversation heading. */
function status(page: Page) {
  return page.locator(".stat").filter({ hasText: "Status" }).locator(".stat-value");
}

for (const runner of ["codex", "claude"]) {
  test(`a new ${runner} chat streams its turn and records an approval`, async ({
    page,
    engine,
  }, testInfo) => {
    engine.script(SCRIPT);

    await page.goto("/conversations");
    await page.getByLabel("Runner").selectOption(runner);
    await page.getByLabel("Message the agent").fill("Write the greeting file.");
    await page.getByRole("button", { name: "Send" }).click();

    // Streaming, asserted while it is still happening: the turn is paused on a
    // question, and what the agent said and did before asking it is already on
    // screen rather than arriving when the turn ends.
    await expect(page.getByText("Reading the repository first.")).toBeVisible();
    await shot(page, testInfo, "1 mid-turn, streaming");
    const pending = page.locator(".approval-pending");
    await expect(pending).toContainText(COMMAND);
    await expect(status(page)).toHaveText("Live");
    await shot(page, testInfo, "2 the approval, pending");

    await pending.getByRole("button", { name: "Approve", exact: true }).click();

    await expect(page.locator(".approval-decided")).toContainText(`Approved · ${COMMAND}`);
    await expect(page.getByText("Wrote the file.")).toBeVisible();
    await expect(status(page)).toHaveText("Idle");
    await shot(page, testInfo, "3 the approval, decided");

    const { threads } = await (await page.request.get("/api/threads")).json();
    expect(threads).toHaveLength(1);
    const [thread] = threads;
    expect(thread.runner).toBe(runner);
    // The naming turn runs on the same CLI over its non-interactive transport,
    // so a title is evidence that path works too.
    expect(thread.title).toBe(SCRIPT.title);
    // The file, not the event: a provider that acknowledges an approval and
    // then does nothing has failed at the only thing the approval was for.
    expect(existsSync(path.join(thread.workspaceRoot, "allowed.txt"))).toBe(true);
  });
}
