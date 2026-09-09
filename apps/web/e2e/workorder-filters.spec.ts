import { expect, shot, test } from "./harness";

test("WorkOrder filters stay in the viewport with Projects expanded", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/runs");
  const projects = page.getByRole("button", { name: "Projects", exact: true });
  if (await projects.getAttribute("aria-expanded") !== "true") await projects.click();
  await expect(projects).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: "WorkOrders", exact: true }))
    .toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Filter WorkOrders", exact: true }).click();

  const menu = page.getByRole("group", { name: "WorkOrder filters" });
  for (const name of ["Implementation", "Review", "Human Review", "failed", "succeeded"]) {
    const option = menu.locator("label").filter({ has: page.getByRole("checkbox", { name, exact: true }) });
    await expect(option).toBeInViewport({ ratio: 1 });
  }
  await menu.getByRole("checkbox", { name: "succeeded" }).uncheck();
  await expect(menu.getByRole("checkbox", { name: "succeeded" })).not.toBeChecked();
  await shot(page, testInfo, "filters above collapsed WorkOrders");
});
