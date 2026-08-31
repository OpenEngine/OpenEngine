/** Where a request is shown, which is half of what it says.
 *
 *  A card rendered perfectly at the end of a turn tells the reader the agent
 *  asked about the last thing it did. The pairing is made by the provider's own
 *  id for the call, and the run-bound MCP server is the one place that id has to
 *  be looked up rather than known -- it is reached over a transport of its own,
 *  and the number it numbers a request with is not anything the transcript
 *  contains. So this drives the tools that go through it and reads the rendered
 *  order, per runner, because each provider spells its call ids differently.
 *
 *  The matching itself is pinned at speed in
 *  `tests/test_workflow_mcp_execution.py`; what only a browser can say is that
 *  the pairing survives everything between the broker and the rendered page.
 */

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const TASK = "Add a greeting file to the repository.";
const PULL_REQUEST = "https://github.com/acme/api/pull/7";

/** Three commands, and not all distinct.
 *
 *  Two *different* ones, because an id that anchors nothing and an id that
 *  anchors the wrong call both read as "inline" when there is only one call on
 *  screen to be beside. Then a repeat of the first, because identical calls are
 *  the only input where the newest-first lookup can pair with the wrong one --
 *  and a run that never repeats a command would never show it. */
const FIRST = ["status", "--short"];
const SECOND = ["log", "--oneline", "-1"];
const THIRD = FIRST;

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
        { type: "tool", name: "git_subcommand", arguments: { arguments: THIRD } },
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

/** The approval slot belonging to one call that named `argument`.
 *
 *  Reached through the call itself -- the sibling of the `details` whose
 *  arguments carry it -- rather than by asking for approvals and hoping the
 *  right one comes back. That adjacency is the whole property under test.
 *  `occurrence` is which of the identically-spelled calls is meant, in the
 *  order the transcript records them. */
function approvalUnderCall(page: Page, argument: string, occurrence = 0) {
  return page
    .locator(`details.tool:has(pre:has-text("${argument}")) + .approvals-call`)
    .nth(occurrence);
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

    // The third is the first command again, and the two are told apart only by
    // the id the provider gave each call. One card under each is the assertion:
    // a lookup that paired both with the same call leaves this one holding two
    // and the earlier one holding none.
    const third = approvalUnderCall(page, THIRD[1], 1);
    await expect(third.locator(".approval-pending")).toContainText(
      `git ${THIRD.join(" ")}`,
    );
    await expect(first.locator(".approval")).toHaveCount(1);
    await expect(third.locator(".approval")).toHaveCount(1);
    await expect(unplacedApprovals(page)).toHaveCount(0);
    await shot(page, testInfo, "3 the repeat, beside the call that repeated it");
    await third.getByRole("button", { name: "Approve", exact: true }).click();

    // Reload rather than re-render: the pairing is the provider's, so it has to
    // survive being read back from the store instead of from the run's stream.
    await expect(third.locator(".approval-decided")).toBeVisible();
    await page.reload();
    for (const [command, occurrence] of [
      [FIRST, 0],
      [SECOND, 0],
      [THIRD, 1],
    ] as const) {
      const slot = approvalUnderCall(page, command[1], occurrence);
      await expect(slot.locator(".approval-decided")).toContainText(
        `Approved · git ${command.join(" ")}`,
      );
      await expect(slot.locator(".approval")).toHaveCount(1);
    }
    await expect(unplacedApprovals(page)).toHaveCount(0);
    await shot(page, testInfo, "4 all three requests, after a reload");
  });
}
