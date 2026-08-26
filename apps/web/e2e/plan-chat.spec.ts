import { expect, shot, test, type Script } from "./harness";

const PROJECT_NAME = "Planning the greeting file";
// Long enough to be several lines of tooltip, which is what a planner writes
// and what the band above a milestone cannot hold.
const FOUNDATION_DESCRIPTION =
  "Establish the project structure and the shared planning model every later " +
  "milestone hangs off, including the store, the API, and the timeline the " +
  "plan is written into.";
const SCRIPT: Script = {
  title: PROJECT_NAME,
  scenarios: [
    // Listed first: the scenario below matches any turn, this one only the
    // second message.
    {
      when: "one more",
      steps: [
        {
          type: "tool",
          name: "add_milestone",
          arguments: {
            name: "Wider rollout",
            description: "Offer the greeting file to every team.",
          },
        },
        { type: "say", text: "Added one more." },
      ],
    },
    {
      steps: [
        {
          type: "tool",
          name: "add_milestone",
          arguments: {
            name: "Planning foundation",
            description: FOUNDATION_DESCRIPTION,
          },
        },
        {
          type: "tool",
          name: "add_milestone",
          arguments: {
            name: "First release",
            description: "Deliver the planned experience to its first users.",
          },
        },
        { type: "say", text: "Here is what I would change." },
      ],
    },
  ],
};

test("a new project opens a planning conversation and appears in the rail", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/conversations");
  await page.getByRole("button", { name: "Projects" }).click();
  await page.getByRole("link", { name: "+ New project" }).click();

  // The new conversation page, on the agent that plans rather than the one
  // that codes -- which is the whole of what the button settles.
  await expect(page).toHaveURL(/\/plan$/);
  await expect(
    page.getByRole("heading", { name: "Define a new project with milestones" }),
  ).toBeVisible();
  await expect(page.getByText("Start a conversation.")).toHaveCount(0);
  await expect(page.getByLabel("Message the agent")).toHaveAttribute(
    "placeholder",
    "Tell the agent about the project you're working on..",
  );
  // Named by the field's own label, which a browser reads as the label text
  // followed by what the control offers -- hence the anchor rather than a
  // whole name, and hence not `getByLabel("Agent")`, which the composer's
  // "Message the agent" also answers to.
  await expect(page.getByRole("combobox", { name: /^Agent/ })).toHaveValue("planner");
  await expect(page.getByText("Turns", { exact: true })).toHaveCount(0);
  const timeline = page.getByRole("region", { name: "Milestone timeline" });
  await expect(timeline).toHaveClass(/milestone-timeline-collapsed/);
  await shot(page, testInfo, "1 the plan page");

  let releaseTitle!: () => void;
  const titleBlocked = new Promise<void>((resolve) => {
    releaseTitle = resolve;
  });
  await page.route(/\/api\/threads\/[^/]+\/title$/, async (route) => {
    await titleBlocked;
    await route.continue();
  });

  await page.getByLabel("Message the agent").fill("How would you add a greeting file?");
  await page.getByRole("button", { name: "Send" }).click();

  // Force the header GET to win the race with title generation. The active
  // assistant-ui item still has its optimistic local ID at this point.
  await expect(page.getByRole("heading", { name: "New project" })).toBeVisible();
  releaseTitle();
  await expect(page.getByText("Here is what I would change.")).toBeVisible();
  await expect(page.getByText("This project", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: PROJECT_NAME })).toBeVisible();
  await expect(
    page.getByText("This runner answers here until you pick another.", { exact: true }),
  ).toHaveCount(0);
  await expect(timeline).toHaveClass(/milestone-timeline-expanded/);
  await expect(page.getByText("Planning foundation", { exact: true })).toBeVisible();
  await expect(page.getByText("First release", { exact: true })).toBeVisible();
  await shot(page, testInfo, "2 the planner answers");

  await page.getByText("Planning foundation", { exact: true }).hover();
  const description = page.getByRole("tooltip", { name: FOUNDATION_DESCRIPTION });
  await expect(description).toBeVisible();
  // Whole, not merely rendered: the tooltip hangs off a node inside a box that
  // scrolls, and used to be cut off by it -- which `toBeVisible` still allows.
  await expect(description).toBeInViewport({ ratio: 1 });

  // The timeline follows the plan as it is written: a milestone added to a
  // project already on screen arrives without the page being reloaded.
  await page.getByLabel("Message the agent").fill("Could you add one more milestone?");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("Added one more.")).toBeVisible();
  await expect(page.getByText("Wider rollout", { exact: true })).toBeVisible();

  const { projects } = await (await page.request.get("/api/projects")).json();
  expect(projects).toHaveLength(1);
  expect(projects[0].name).toBe(PROJECT_NAME);
  // The plan page opens on Projects, so the section that lists the new project
  // is already open -- clicking its header here would close it.
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
  // What the poll showed was written down: every milestone, including the one
  // added mid-conversation, is read back from the store on a cold load.
  await expect(page.getByText("Planning foundation", { exact: true })).toBeVisible();
  await expect(page.getByText("First release", { exact: true })).toBeVisible();
  await expect(page.getByText("Wider rollout", { exact: true })).toBeVisible();
  await page.getByText("Planning foundation", { exact: true }).hover();
  await expect(page.getByRole("tooltip", { name: FOUNDATION_DESCRIPTION })).toBeVisible();

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
  // Arriving on a plan, the rail opens on Projects with that one marked, rather
  // than on Chats with the row you just clicked folded out of sight.
  await expect(page.getByRole("button", { name: "Projects" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(
    page
      .getByRole("navigation", { name: "Projects" })
      .getByRole("link", { name: PROJECT_NAME }),
  ).toHaveAttribute("aria-current", "page");
  await shot(page, testInfo, "3 the project reopens its plan");

  // A plan is more than the conversation that wrote it. The project offers its
  // milestones under its own row, and the page they open reads the timeline
  // first and then says what each goal actually is.
  const rail = page.getByRole("navigation", { name: "Projects" });
  await rail.getByRole("link", { name: "Milestones · 3" }).click();

  await expect(page).toHaveURL(/\/projects\/[^/]+\/milestones$/);
  await expect(page.getByRole("heading", { name: "Milestones", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Milestone timeline" })).toBeVisible();
  // The description a node can only offer on a hover, read here in full and in
  // flow -- which is the whole reason this page exists.
  await expect(page.getByRole("article", { name: "Planning foundation" })).toContainText(
    FOUNDATION_DESCRIPTION,
  );
  await expect(page.getByRole("article", { name: "First release" })).toBeVisible();
  await expect(page.getByRole("article", { name: "Wider rollout" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Milestones · 3" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await shot(page, testInfo, "4 the project's milestones");

  // And the way back to the conversation the plan was written in.
  await page.getByRole("link", { name: "← Planning conversation" }).click();
  await expect(page).toHaveURL(`/conversations/${threads[0].id}`);
});

test("a project is archived into its own list and restored from it", async ({
  page,
  engine,
}, testInfo) => {
  engine.script({
    title: PROJECT_NAME,
    scenarios: [{ steps: [{ type: "say", text: "Here is what I would change." }] }],
  });

  await page.goto("/plan");
  await page.getByLabel("Message the agent").fill("How would you add a greeting file?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Here is what I would change.")).toBeVisible();

  const rail = page.getByRole("navigation", { name: "Projects" });
  await expect(rail.getByRole("link", { name: PROJECT_NAME })).toBeVisible();
  await expect(page.getByText("Archived projects")).toHaveCount(0);

  await rail.getByRole("button", { name: `Archive ${PROJECT_NAME}` }).click();

  // Put away, not deleted: the row leaves the list for a submenu that is shut
  // until it is asked for, which is where the project has gone rather than
  // where it is on screen.
  await expect(page.getByText("Archived projects")).toBeVisible();
  await expect(rail.getByText(PROJECT_NAME)).toBeHidden();
  await shot(page, testInfo, "5 the project is archived");

  const archived = await (await page.request.get("/api/projects")).json();
  expect(archived.projects[0].archived).toBe(true);

  await page.getByText("Archived projects").click();

  // Open, the row is the plain one a project with no conversation gets: an
  // archived project is not somewhere to click through to.
  await expect(rail.getByText(PROJECT_NAME)).toBeVisible();
  await expect(rail.getByRole("link", { name: PROJECT_NAME })).toHaveCount(0);
  await shot(page, testInfo, "6 the archived projects submenu");

  await rail.getByRole("button", { name: `Restore ${PROJECT_NAME}` }).click();

  await expect(rail.getByRole("link", { name: PROJECT_NAME })).toBeVisible();
  await expect(page.getByText("Archived projects")).toHaveCount(0);
  // Written down rather than only redrawn, so a reload finds it back as well.
  await page.reload();
  await expect(rail.getByRole("link", { name: PROJECT_NAME })).toBeVisible();
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
