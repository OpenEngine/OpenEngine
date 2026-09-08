/** Steering a graph agent while its current turn is still executing.
 *
 * The existing graph e2e coverage sends steering while the agent is waiting
 * for an approval, then answers that approval to let the turn finish. The
 * reported failure happened earlier: the person sent guidance specifically to
 * interrupt work already in progress, but the agent did not respond. */

import { writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Script } from "./harness";

const WORKFLOW = "[BETA] Implementation review (codex)";
const TASK = "Keep working until I send guidance.";
const STEERING = "Interrupt the current work and acknowledge this guidance.";
const WORKING = "I am working on the initial task.";
const ACKNOWLEDGEMENT = "I received the mid-execution guidance.";
const RELEASE = "release-initial-turn";
const COMMAND = `until [ -f ${RELEASE} ]; do sleep 0.05; done`;

const SCRIPT: Script = {
  title: "Waiting for guidance",
  scenarios: [
    {
      when: STEERING,
      steps: [{ type: "say", text: ACKNOWLEDGEMENT }],
    },
    {
      when: "Review the implementation",
      steps: [{ type: "say", text: "The implementation is ready for review." }],
    },
    {
      when: TASK,
      steps: [
        { type: "say", text: WORKING },
        { type: "run", command: COMMAND, approval: false },
      ],
    },
  ],
};

test("a graph agent responds when steered during an executing turn", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  await page.goto("/runs/new");
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  await page.getByLabel("Repository").fill(engine.repository);
  await page.getByLabel("Task prompt").fill(TASK);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);

  const runUrl = new URL(page.url()).pathname;
  const runId = runUrl.split("/").pop() ?? "";
  const workspace = await expect
    .poll(async () => {
      const response = await page.request.get(`/graph/api/runs/${runId}`);
      const run = await response.json();
      return typeof run.values?.workspace === "string" ? run.values.workspace : "";
    })
    .not.toBe("")
    .then(async () => {
      const response = await page.request.get(`/graph/api/runs/${runId}`);
      return String((await response.json()).values.workspace);
    });

  try {
    await page.goto(runUrl);
    await page
      .locator(".step")
      .filter({ has: page.getByRole("heading", { name: "Implementation", exact: true }) })
      .getByRole("link", { name: "Open conversation" })
      .click();
    await expect(page.getByText(/This node's agent is working/)).toBeVisible();

    await page.getByLabel("Message the agent").fill(STEERING);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(STEERING)).toBeVisible();

    // Steering is accepted and appears in the transcript, but the active ACP
    // turn remains blocked in COMMAND. The guidance therefore cannot produce
    // this response, which is the reported failure.
    await expect(page.getByText(ACKNOWLEDGEMENT)).toBeVisible({ timeout: 5_000 });
  } finally {
    // Let the provider process exit cleanly even when the assertion above
    // proves that steering did not interrupt it.
    writeFileSync(path.join(workspace, RELEASE), "go", "utf-8");
  }
});
