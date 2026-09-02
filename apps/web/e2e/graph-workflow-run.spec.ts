/** `workflow-run.spec.ts`, migrated to the graph control surface.
 *
 *  The same run, the same browser, the same evidence -- a checkout that exists
 *  on disk, a command streaming while the step is still working, and a decision
 *  only a person can make -- against `engine.graph_runtime` instead of the
 *  workflow API. The server is the composed application with
 *  pointed at a workflow variant whose definitions run as graphs -- which is
 *  how a deployment chooses too -- and the agents are
 *  `tests/acp_provider_fakes.py` reading the same script the Codex and Claude
 *  fakes read.
 *
 *  Two things the other spec asserts have no counterpart here, and their
 *  absence is the point of the migration rather than a gap in it:
 *
 *  * `complete_step`. A graph node ends when its ACP turn ends, so nothing has
 *    to be called to end one and there is no run-bound MCP server attached. The
 *    agent's own last words are the node's output, which is what
 *    `.step-outputs` shows.
 *  * The reviewer's `gh` comment. That is the review agent's MCP tooling, which
 *    goes with the MCP server; what the reviewer says still reaches the page.
 *
 *  What replaces them is the thing the graph runtime has and the workflow API
 *  did not: the human decision is an approval raised by a node that is still
 *  running, so approving it releases that node rather than writing a record
 *  beside a finished run. */

import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

const TASK = "Add a greeting file to the repository.";
const FINDING = "greeting.txt is not covered by a test.";
const DECISION = "The finding can wait; ship the greeting.";

/** What the implementation waits for before writing anything.
 *
 *  A file the test creates rather than a sleep, for the reason the workflow
 *  spec gives: the streaming assertion is about what is on screen *while* the
 *  step runs, and a race decided by a timer fails in the direction that hides a
 *  broken stream. Relative, because it runs in the run's own checkout -- which
 *  is also what makes the path the page names checkable. */
const RELEASE = "go";
const COMMAND = `until [ -f ${RELEASE} ]; do sleep 0.05; done; echo hello > greeting.txt`;

const SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    // The reviewer is quoted the original task, so its prompt contains the
    // implementation scenario's word too. First match wins, so the one only a
    // reviewer can match goes first.
    {
      when: "Review the implementation",
      steps: [{ type: "say", text: `Read the change. ${FINDING}` }],
    },
    {
      when: "greeting",
      steps: [
        { type: "say", text: "Writing the greeting." },
        { type: "run", command: COMMAND, approval: false },
      ],
    },
  ],
};

/** One stage's card on the run page, by the name the topology gives it.
 *
 *  Exactly, because "Review" is also how "Human review" starts. */
function step(page: Page, name: string) {
  return page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

test.use({ graphRuntime: true });

for (const runner of ["codex", "claude"]) {
  test(`a ${runner} graph run provisions, streams, and is approved`, async ({
    page,
    engine,
  }, testInfo) => {
    engine.script(SCRIPT);

    await page.goto("/runs/new");
    await page.getByLabel("Repository").fill(engine.repository);
    await page.getByLabel("Implementation runner").selectOption(runner);
    await page.getByLabel("Task prompt").fill(TASK);
    await page.getByRole("button", { name: "Create WorkOrder" }).click();

    await expect(page).toHaveURL(/\/runs\/run-/);
    const runUrl = new URL(page.url()).pathname;

    // Provisioning is a node, so it is a position the run stands at and then
    // leaves: the workspace stage goes behind it and the checkout it names is a
    // directory that exists.
    await expect(page.locator(".stages .stage").first()).toHaveAttribute(
      "data-status",
      "completed",
    );
    await expect(page.locator(".detail-title .chip")).toHaveText("Implementation");
    const checkout = page.locator(".run-workspace .dock-path");
    await expect(checkout).toContainText("cd ");
    const workspace = ((await checkout.textContent()) ?? "").replace(/^cd\s*/, "").trim();
    expect(existsSync(workspace)).toBe(true);
    await shot(page, testInfo, "1 provisioned");

    // Streaming, asserted while the step is still running: the command is on
    // screen before it has finished, so what is shown arrived over the run's
    // event feed rather than with the page.
    await expect(step(page, "Implementation").locator(".tool-detail")).toHaveText(
      COMMAND,
    );
    await expect(step(page, "Implementation")).toHaveAttribute("data-live", "true");
    await shot(page, testInfo, "2 mid-step, streaming");

    // Let the command finish. The file the agent then writes is the evidence
    // that the path the run page named is the one it is working in.
    writeFileSync(path.join(workspace, RELEASE), "", "utf-8");
    await expect
      .poll(() => existsSync(path.join(workspace, "greeting.txt")))
      .toBe(true);

    // Advancing: the implementation's own words are its output, the review ran
    // on the reviewer, and the run stops where a person has to decide.
    await page.goto(runUrl);
    await expect(page.locator(".detail-title .chip")).toHaveText("Human review");
    await expect(step(page, "Implementation").locator(".step-outputs")).toContainText(
      "Writing the greeting.",
    );
    await expect(step(page, "Review").locator(".step-outputs")).toContainText(FINDING);
    await expect(page.locator(".callout-action")).toContainText("Action required");
    await shot(page, testInfo, "3 review reached");

    // The decision, made the way a person makes it. The note goes to the
    // execution that is waiting -- steering, not a field on the decision -- and
    // the approval releases the node that raised it.
    await page.getByLabel("Decision note").fill(DECISION);
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
    await shot(page, testInfo, "4 approved");

    // Reload rather than re-render: the end state is the checkpointer's, so it
    // has to survive the page that submitted it going away.
    await page.reload();
    await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
    await expect(page.locator(".stats")).toContainText("approved");
    const stages = page.locator(".stages .stage");
    await expect(stages).toHaveText([
      "Workspace",
      "Implementation",
      "Review",
      "Human review",
    ]);
    for (const index of [0, 1, 2, 3])
      await expect(stages.nth(index)).toHaveAttribute("data-status", "completed");
    await expect(step(page, "Human review")).toContainText(DECISION);
    await expect(page.locator(".callout-action")).toHaveCount(0);
  });
}
