import { expect, shot, test } from "./harness";

test("WorkOrder filters stay in the viewport with Projects expanded", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const stages = Array.from({ length: 30 }, (_, index) => `Custom stage ${index + 1}`);
  const options = ["Implementation", "Review", "Human Review", "failed", "succeeded", ...stages];
  await page.route("**/api/runs", (route) => route.fulfill({ json: {
    runs: options.map((name) => ({
      runId: name, name, workflowId: "work", workflowName: "Work", workflowVersion: "v1",
      taskId: name, workstreamId: null, milestoneId: null, repository: ".",
      repositoryContext: { repository: "." }, terminalOutcome: null,
      phase: ["failed", "succeeded"].includes(name) ? name : "running_agent",
      currentStepId: name,
      steps: [{ stepId: name, name, kind: "agent", status: "in_progress", outcome: null,
        changesRequested: false, agentId: null, agentInstanceId: null, agentRunId: null,
        conversationId: null, conversationUrl: null, waiting: false }],
    })),
  } }));
  await page.goto("/runs");
  const projects = page.getByRole("button", { name: "Projects", exact: true });
  if (await projects.getAttribute("aria-expanded") !== "true") await projects.click();
  await expect(projects).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: "WorkOrders", exact: true }))
    .toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Filter WorkOrders", exact: true }).click();

  const menu = page.getByRole("group", { name: "WorkOrder filters" });
  await expect(menu).toBeInViewport({ ratio: 1 });
  expect(await menu.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  for (const name of options) {
    const checkbox = menu.getByRole("checkbox", { name, exact: true });
    await checkbox.scrollIntoViewIfNeeded();
    await expect(checkbox).toBeInViewport({ ratio: 1 });
    await checkbox.uncheck();
    await expect(checkbox).not.toBeChecked();
  }
  await shot(page, testInfo, "filters above collapsed WorkOrders");
});
