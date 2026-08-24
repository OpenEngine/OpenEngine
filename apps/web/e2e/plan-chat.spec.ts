import { expect, shot, test, type Script } from "./harness";

const PROJECT_NAME = "Planning the greeting file";
const SCRIPT: Script = {
  title: PROJECT_NAME,
  scenarios: [{ steps: [{ type: "say", text: "Here is what I would change." }] }],
};

test("a new project opens a planning conversation and appears in the rail", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/conversations");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("link", { name: "New Project" }).click();

  // The new conversation page, on the agent that plans rather than the one
  // that codes -- which is the whole of what the button settles.
  await expect(page).toHaveURL(/\/plan$/);
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  // Named by the field's own label, which a browser reads as the label text
  // followed by what the control offers -- hence the anchor rather than a
  // whole name, and hence not `getByLabel("Agent")`, which the composer's
  // "Message the agent" also answers to.
  await expect(page.getByRole("combobox", { name: /^Agent/ })).toHaveValue("planner");
  await shot(page, testInfo, "1 the plan page");

  await page.getByLabel("Message the agent").fill("How would you add a greeting file?");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("Here is what I would change.")).toBeVisible();
  await shot(page, testInfo, "2 the planner answers");

  const { projects } = await (await page.request.get("/api/projects")).json();
  expect(projects).toHaveLength(1);
  expect(projects[0].name).toBe(PROJECT_NAME);
  await page.getByRole("button", { name: "Projects" }).click();
  await expect(
    page.getByRole("navigation", { name: "Projects" }).getByText(PROJECT_NAME),
  ).toBeVisible();

  const { threads } = await (await page.request.get("/api/threads")).json();
  expect(threads).toHaveLength(1);
  // The chat is an ordinary one, listed beside the others: what a plan starts
  // is a conversation, and only its agent is different.
  expect(threads[0].agentId).toBe("planner");
  // And it now has a URL of its own, so a refresh reopens the plan being
  // written rather than starting a second empty one.
  await expect(page).toHaveURL(`/conversations/${threads[0].id}`);
  await page.reload();
  await expect(page.getByText("Here is what I would change.")).toBeVisible();

  // Leave the plan behind, then come back to it the way the rail offers: the
  // project row opens the conversation it was named after.
  await page.goto("/runs");
  await page.getByRole("button", { name: "Projects" }).click();
  await page
    .getByRole("navigation", { name: "Projects" })
    .getByRole("link", { name: PROJECT_NAME })
    .click();

  await expect(page).toHaveURL(`/conversations/${threads[0].id}`);
  await expect(page.getByText("Here is what I would change.")).toBeVisible();
  await shot(page, testInfo, "3 the project reopens its plan");
});

test("a new chat started from the plan page is not another plan", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  await page.goto("/plan");
  await page.getByRole("button", { name: "Chats" }).click();
  await page.getByRole("link", { name: "+ New chat" }).click();

  // The plan page's defaults are a plan's, so the control that starts an
  // ordinary chat has to leave rather than reuse them where it stands.
  await expect(page).toHaveURL(/\/conversations$/);
  await expect(page.getByRole("combobox", { name: /^Agent/ })).toHaveValue("coder");
});

test("new project intent survives closing while its title is pending", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  let releaseTitle!: () => void;
  const titleBlocked = new Promise<void>((resolve) => {
    releaseTitle = resolve;
  });
  let titleStarted!: () => void;
  const titleRequested = new Promise<void>((resolve) => {
    titleStarted = resolve;
  });
  const titleUrl = /\/api\/threads\/[^/]+\/title$/;
  await page.route(titleUrl, async (route) => {
    titleStarted();
    await titleBlocked;
    await route.abort().catch(() => {});
  });

  await page.goto("/plan");
  await page.getByLabel("Message the agent").fill("Keep this project after reload");
  await page.getByRole("button", { name: "Send" }).click();
  await titleRequested;
  await expect(page).toHaveURL(/\/conversations\/agi-/);

  const beforeReload = await (await page.request.get("/api/projects")).json();
  expect(beforeReload.projects).toHaveLength(1);
  expect(beforeReload.projects[0].name).toBe("New project");

  const permalink = page.url();
  await page.close();
  releaseTitle();
  const reopened = await page.context().newPage();
  await reopened.goto(permalink);

  engine.script({
    title: PROJECT_NAME,
    scenarios: [{ steps: [{ type: "say", text: "Recovered after closing." }] }],
  });

  await reopened.getByLabel("Message the agent").fill("Keep this project after reload");
  await reopened.getByRole("button", { name: "Send" }).click();
  await expect(reopened.getByText("Recovered after closing.")).toBeVisible();

  const afterReload = await (await reopened.request.get("/api/projects")).json();
  expect(afterReload.projects).toHaveLength(1);
  expect(afterReload.projects[0].name).toBe(PROJECT_NAME);
  expect(afterReload.projects[0].projectId).toBe(beforeReload.projects[0].projectId);
});
