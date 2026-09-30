import { expect, shot, test } from "./harness";

test("WorkOrder filters stay in the viewport with WorkOrders collapsed", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const stages = Array.from({ length: 30 }, (_, index) => `Custom stage ${index + 1}`);
  const options = ["Implementation", "Review", "Human Review", "failed", "succeeded", ...stages];
  await page.route("**/api/runs", (route) => route.fulfill({ json: {
    runs: options.map((name) => ({
      runId: name, name, workflowId: "work", workflowName: "Work",
      taskId: name, repository: ".",
      repositoryContext: { repository: "." }, terminalOutcome: null,
      phase: ["failed", "succeeded"].includes(name) ? name : "running_agent",
      graphProgress: { activeNodeIds: [name], waitingNodeIds: [], nextNodeIds: [] },
    })),
  } }));
  await page.goto("/runs");
  const workorders = page.getByRole("button", { name: "WorkOrders", exact: true });
  await workorders.click();
  await expect(workorders).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Filter WorkOrders", exact: true }).click();

  const menu = page.getByRole("group", { name: "WorkOrder filters" });
  // The accepted run is archived, and its stage is only an option once the
  // archive is in the rail.
  await menu.getByRole("checkbox", { name: "Archived", exact: true }).check();
  await expect(menu).toBeInViewport({ ratio: 1 });
  expect(await menu.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  for (const name of options) {
    const checkbox = menu.getByRole("checkbox", { name, exact: true });
    await checkbox.scrollIntoViewIfNeeded();
    await expect(checkbox).toBeInViewport({ ratio: 1 });
    await checkbox.uncheck();
    await expect(checkbox).not.toBeChecked();
  }
  await shot(page, testInfo, "filters beside collapsed WorkOrders");
});
