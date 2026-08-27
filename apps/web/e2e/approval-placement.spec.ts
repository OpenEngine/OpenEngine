/** Where a request is shown, which is half of what it says.
 *
 *  A card rendered perfectly at the end of a turn tells the reader the agent
 *  asked about the last thing it did. The pairing is made by the provider's own
 *  id for the call, and the run-bound MCP server is the one place that id has to
 *  be looked up rather than known -- it is reached over a transport of its own,
 *  and the number it numbers a request with is not anything the transcript
 *  contains. So this drives the tools that go through it and reads the rendered
 *  order, per runner, because each provider spells its call ids differently.
 */

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const TASK = "Add a greeting file to the repository.";
const PULL_REQUEST = "https://github.com/acme/api/pull/7";

/** Two commands rather than one, and different ones: an id that anchors
 *  nothing and an id that anchors the wrong call both read as "inline" when
 *  there is only one call on screen to be beside. */
const FIRST = ["status", "--short"];
const SECOND = ["log", "--oneline", "-1"];

const SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    {
      when: "Inspect the workspace",
      steps: [
        { type: "say", text: "Read the change." },
        {
          type: "tool",
          name: "add_comment",
          arguments: { pr_url: PULL_REQUEST, comment: "Looks right." },
        },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Reviewed the greeting.",
            outputs: { findings: "none" },
          },
        },
      ],
    },
    {
      when: "greeting",
      steps: [
        { type: "say", text: "Checking the checkout first." },
        { type: "tool", name: "git_subcommand", arguments: { arguments: FIRST } },
        { type: "tool", name: "git_subcommand", arguments: { arguments: SECOND } },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Added the greeting.",
            outputs: { pr_url: PULL_REQUEST },
          },
        },
      ],
    },
  ],
};

/** The approval slot belonging to the call that named `argument`.
 *
 *  Reached through the call itself -- the sibling of the `details` whose
 *  arguments carry it -- rather than by asking for approvals and hoping the
 *  right one comes back. That adjacency is the whole property under test. */
function approvalUnderCall(page: Page, argument: string) {
  return page.locator(
    `details.tool:has(pre:has-text("${argument}")) + .approvals-call`,
  );
}

/** Requests the client could not place: the ones a regression collects here. */
function unplacedApprovals(page: Page) {
  return page.locator(".approvals:not(.approvals-call)");
}

for (const runner of ["codex", "claude"]) {
  test(`a ${runner} step shows each repository approval beside its call`, async ({
    page,
    engine,
  }, testInfo) => {
    engine.script(SCRIPT);

    await page.goto("/runs/new");
    await page.getByLabel("Repository").fill(engine.repository);
    await page.getByLabel("Implementation runner").selectOption(runner);
    await page.getByLabel("Task prompt").fill(TASK);
    await page.getByRole("button", { name: "Create workflow run" }).click();

    await expect(page).toHaveURL(/\/runs\/run-/);
    await page
      .locator(".step")
      .filter({ has: page.getByRole("heading", { name: "Implementation", exact: true }) })
      .getByRole("link", { name: "Open conversation" })
      .click();
    await expect(page).toHaveURL(/\/conversations\//);

    // The first pause, under the first call and nowhere else.
    const first = approvalUnderCall(page, FIRST[1]);
    await expect(first.locator(".approval-pending")).toContainText(
      `git ${FIRST.join(" ")}`,
    );
    await expect(unplacedApprovals(page)).toHaveCount(0);
    await shot(page, testInfo, "1 the first request, beside its call");
    await first.getByRole("button", { name: "Approve", exact: true }).click();

    // The second pause is about a different command, so it has to move rather
    // than accumulate: the answered one stays where it was answered.
    const second = approvalUnderCall(page, SECOND[1]);
    await expect(second.locator(".approval-pending")).toContainText(
      `git ${SECOND.join(" ")}`,
    );
    await expect(first.locator(".approval-decided")).toContainText(
      `Approved · git ${FIRST.join(" ")}`,
    );
    await expect(unplacedApprovals(page)).toHaveCount(0);
    await shot(page, testInfo, "2 the second request, beside its own call");
    await second.getByRole("button", { name: "Approve", exact: true }).click();

    // Reload rather than re-render: the pairing is the provider's, so it has to
    // survive being read back from the store instead of from the run's stream.
    await expect(second.locator(".approval-decided")).toBeVisible();
    await page.reload();
    await expect(approvalUnderCall(page, FIRST[1]).locator(".approval-decided")).toContainText(
      `Approved · git ${FIRST.join(" ")}`,
    );
    await expect(approvalUnderCall(page, SECOND[1]).locator(".approval-decided")).toContainText(
      `Approved · git ${SECOND.join(" ")}`,
    );
    await expect(unplacedApprovals(page)).toHaveCount(0);
    await shot(page, testInfo, "3 both requests, after a reload");
  });
}
