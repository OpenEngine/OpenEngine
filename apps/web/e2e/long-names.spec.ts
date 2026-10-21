/** A conversation stays readable however long the name above it is.
 *
 *  A WorkOrder its naming agent could not name is called by its whole task,
 *  which for one started from an issue brief runs to hundreds of characters,
 *  and a chat's generated title can run to eighty. Either sits in
 *  the conversation's header, and a header that grows with its name pushes the
 *  chat below it off the screen. The name stops at about three lines instead,
 *  the way the run and loop pages already cap theirs (#930), and the whole of
 *  it stays on the heading's `title` for anyone who wants to read it. */

import type { Locator, Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const WORKFLOW = "Implementation review rerank";
const NAMING_REQUEST = "Give this WorkOrder a concise display name";
const GREETING = "greeting.txt";

/** A task pasted in from an issue brief, and so the WorkOrder's name: far more
 *  than any header has room for. */
const LONG_TASK = [
  "#978 Cap the conversation header title so that a long WorkOrder name does",
  "not cover the chat underneath it when the WorkOrder was started from an",
  "issue brief whose instructions were pasted in whole, including every",
  "acceptance criterion, every note about prior fixes, every file the change",
  "is expected to touch, the reasoning behind each of them, a reminder that",
  "the problem was found by reading the code rather than by reproducing it in",
  "a browser, and a request to check every other page that uses the same",
  "header for the same problem before calling the work finished",
].join(" ");

/** As long as a chat's title can be: the server keeps the first eighty
 *  characters of what the naming turn answers. */
const LONG_CHAT_TITLE =
  "Investigate why the conversation header keeps growing whenever a title runs long";

const WORKORDER_SCRIPT: Script = {
  scenarios: [
    {
      // Naming is best effort; without a name the WorkOrder keeps its task.
      when: NAMING_REQUEST,
      steps: [{ type: "say", text: "No name comes to mind." }],
    },
    {
      // Stops on a question, so the node is still working and the composer is
      // on the page for as long as the test looks at it.
      when: "Implement the requested change",
      steps: [
        { type: "say", text: "Writing the greeting." },
        { type: "run", command: `echo hello > ${GREETING}`, approval: true },
      ],
    },
  ],
};

const CHAT_SCRIPT: Script = {
  title: LONG_CHAT_TITLE,
  scenarios: [{ steps: [{ type: "say", text: "Reading the header styles." }] }],
};

/** The heading is no taller than three of its own lines. */
async function expectAtMostThreeLines(heading: Locator): Promise<void> {
  const { height, lineHeight } = await heading.evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    lineHeight: parseFloat(getComputedStyle(element).lineHeight),
  }));
  expect(height).toBeLessThanOrEqual(3 * lineHeight + 1);
}

async function createWorkOrder(page: Page, repository: string): Promise<string> {
  await page.goto("/runs/new");
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  await page.getByLabel("Repository").selectOption(repository);
  await page.getByLabel("Task prompt").fill(LONG_TASK);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  return new URL(page.url()).pathname;
}

test("a WorkOrder with a very long name keeps its node's conversation on screen", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(WORKORDER_SCRIPT);

  const runUrl = await createWorkOrder(page, engine.repository);
  await page.goto(`${runUrl}/conversations/graph--implementation`);

  const heading = page.getByRole("heading", { name: LONG_TASK, level: 1 });
  await expect(heading).toBeVisible();
  // The implementer stops to ask, so the person reading this is the one it is
  // waiting on.
  await expect(page.locator(".approval-pending")).toContainText(`echo hello > ${GREETING}`, {
    timeout: 60_000,
  });
  await shot(page, testInfo, "1 a long WorkOrder name above its conversation");

  // The whole name is still there for anyone who points at it.
  await expect(heading).toHaveAttribute("title", LONG_TASK);
  await expectAtMostThreeLines(heading);
  // And the chat is where the person waited on can act on it.
  await expect(page.locator(".approval-pending")).toBeInViewport();
  await expect(page.getByLabel("Message the agent")).toBeInViewport({ ratio: 1 });
});

test("a chat with a long title keeps its conversation on screen", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(CHAT_SCRIPT);
  // A narrower window than a desktop's, where the title column has room for
  // fewer words a line.
  await page.setViewportSize({ width: 1024, height: 640 });

  await page.goto("/conversations");
  await page.getByLabel("Message the agent").fill("Why does the header grow?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Reading the header styles.")).toBeVisible();

  const heading = page.getByRole("heading", { name: LONG_CHAT_TITLE, level: 1 });
  await expect(heading).toBeVisible({ timeout: 30_000 });
  await shot(page, testInfo, "1 a long chat title above its conversation");

  await expect(heading).toHaveAttribute("title", LONG_CHAT_TITLE);
  await expectAtMostThreeLines(heading);
  await expect(page.getByText("Reading the header styles.")).toBeInViewport();
  await expect(page.getByLabel("Message the agent")).toBeInViewport({ ratio: 1 });
});
