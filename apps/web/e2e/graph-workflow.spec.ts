/** The same WorkOrder, run by the graph engine instead of the step executor.
 *
 *  `workflow-run.spec.ts` is one long test, because a step WorkOrder does all
 *  of this and a single spec can walk through it. This one is the same journey
 *  split into the states it passes through, one test each, because a `[BETA]`
 *  WorkOrder does *not* do all of it yet: split, a run reports every gap it
 *  has, where one long test would report only the first.
 *
 *  That is what this file is for. It is expected to fail, its CI job says so,
 *  and each red test names one thing the graph WorkOrder cannot do that the
 *  step one can. Turning them green is the work; deleting them is not.
 *
 *  Everything here is the real thing except the model: a real server, the real
 *  graph engine, real LangGraph, real checkpoint files, a real worktree, and
 *  agents reached over real ACP -- answered by `tests/provider_fakes.py`
 *  instead of by codex or claude. */

import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

/** What the dropdown calls the graph, and what the runner-less form promises. */
const WORKFLOW = "[BETA] Implementation review (codex)";
const TASK = "Add a greeting file to the repository.";
const TITLE = "Adding a greeting";
const NAMING_REQUEST = "Give this WorkOrder a concise display name";
const GREETING = "greeting.txt";
const PULL_REQUEST = "https://github.com/acme/repository/pull/7";
const IMPLEMENTED = "Wrote the greeting.";
const REVIEWED = "Read the change; greeting.txt is not covered by a test.";

const STEER = "Also write a licence file.";
const STEERED = "Wrote the licence.";

const INTERRUPT_TASK = "Keep working until I send guidance.";
const INTERRUPT_STEERING = "Interrupt the current work and acknowledge this guidance.";
const INTERRUPT_WORKING = "I am working on the initial task.";
const ACKNOWLEDGEMENT = "I received the mid-execution guidance.";
const RELEASE = "release-initial-turn";
const BLOCKING_COMMAND = `until [ -f ${RELEASE} ]; do sleep 0.05; done`;

/** The same journey, with the agent stopping to ask before it writes.
 *
 *  Which is the state a conversation has to be readable and answerable in: the
 *  node is in flight, holding its turn, and the person it is waiting on is the
 *  one reading the page. */
const ASKING_SCRIPT: Script = {
  title: TITLE,
  scenarios: [
    { when: NAMING_REQUEST, steps: [{ type: "say", text: JSON.stringify({ name: TITLE }) }] },
    {
      when: "Review the implementation",
      steps: [
        { type: "tool", name: "complete_step", arguments: {
          outcome: "success", summary: "Facet reviewed.", outputs: { findings: "[]" },
        } },
      ],
    },
    {
      when: "You are a senior reviewer consolidating findings",
      steps: [
        { type: "say", text: REVIEWED },
        {
          type: "tool",
          name: "add_comment",
          arguments: { pr_url: PULL_REQUEST, comment: REVIEWED },
        },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: REVIEWED,
            outputs: { findings: "[]" },
          },
        },
      ],
    },
    {
      when: STEER,
      steps: [
        { type: "say", text: STEERED },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Added the requested files.",
            outputs: { pr_url: PULL_REQUEST },
          },
        },
      ],
    },
    {
      when: "Implement the requested change",
      steps: [
        { type: "say", text: "Writing the greeting." },
        { type: "run", command: `echo hello > ${GREETING}`, approval: true },
        { type: "say", text: IMPLEMENTED },
      ],
    },
  ],
};

const SCRIPT: Script = {
  title: TITLE,
  scenarios: [
    { when: NAMING_REQUEST, steps: [{ type: "say", text: JSON.stringify({ name: TITLE }) }] },
    // The reviewer is asked about the implementation *and quoted the original
    // task*, so its prompt contains the implementation's own scenario word.
    // The first match wins, so the one only a reviewer can match goes first.
    {
      when: "Review the implementation",
      steps: [
        { type: "tool", name: "complete_step", arguments: {
          outcome: "success", summary: "Facet reviewed.", outputs: { findings: "[]" },
        } },
      ],
    },
    {
      when: "You are a senior reviewer consolidating findings",
      steps: [
        { type: "say", text: REVIEWED },
        {
          type: "tool",
          name: "add_comment",
          arguments: { pr_url: PULL_REQUEST, comment: REVIEWED },
        },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: REVIEWED,
            outputs: { findings: "[]" },
          },
        },
      ],
    },
    {
      when: "Implement the requested change",
      steps: [
        { type: "say", text: "Writing the greeting." },
        // No approval: what this spec is about is the journey, and an agent
        // stopping to ask permission is a state of its own -- see the last
        // test in this file.
        { type: "run", command: `echo hello > ${GREETING}`, approval: false },
        { type: "say", text: IMPLEMENTED },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: IMPLEMENTED,
            outputs: { pr_url: PULL_REQUEST },
          },
        },
      ],
    },
  ],
};

const STEERING_SCRIPT: Script = {
  title: "Waiting for guidance",
  scenarios: [
    {
      when: NAMING_REQUEST,
      steps: [{ type: "say", text: JSON.stringify({ name: "Waiting for guidance" }) }],
    },
    {
      when: INTERRUPT_STEERING,
      steps: [
        { type: "say", text: ACKNOWLEDGEMENT },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "Applied the human guidance.",
            outputs: { pr_url: PULL_REQUEST },
          },
        },
      ],
    },
    {
      when: "Review the implementation",
      steps: [
        { type: "tool", name: "complete_step", arguments: {
          outcome: "success", summary: "Facet reviewed.", outputs: { findings: "[]" },
        } },
      ],
    },
    {
      when: "You are a senior reviewer consolidating findings",
      steps: [
        { type: "say", text: "The implementation is ready for review." },
        {
          type: "tool",
          name: "add_comment",
          arguments: { pr_url: PULL_REQUEST, comment: "Ready for review." },
        },
        {
          type: "tool",
          name: "complete_step",
          arguments: {
            outcome: "success",
            summary: "The implementation is ready for review.",
            outputs: { findings: "[]" },
          },
        },
      ],
    },
    {
      when: INTERRUPT_TASK,
      steps: [
        { type: "say", text: INTERRUPT_WORKING },
        { type: "run", command: BLOCKING_COMMAND, approval: false },
      ],
    },
  ],
};

/** Create one `[BETA]` WorkOrder and land on its page. */
async function create(
  page: Page,
  repository: string,
  prompt: string = TASK,
): Promise<string> {
  await page.goto("/runs/new");
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  await page.getByLabel("Repository").fill(repository);
  await page.getByLabel("Task prompt").fill(prompt);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  return new URL(page.url()).pathname;
}

/** What the graph engine says about a run, which is the truth the page lags. */
async function graphRun(page: Page, runUrl: string) {
  const runId = runUrl.split("/").pop() ?? "";
  const response = await page.request.get(`/graph/api/runs/${runId}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

test("@beta a graph workflow is offered, and does not ask for a runner", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/runs/new");

  await expect(
    page.getByLabel("Workflow definition").getByRole("option", { name: WORKFLOW }),
  ).toHaveCount(1);
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  // The graph names the agent it runs, so there is nothing to choose.
  await expect(page.getByLabel("Implementation runner")).toHaveCount(0);
  await shot(page, testInfo, "1 the beta choice");
});

test("@beta a graph WorkOrder provisions a checkout and runs its agents", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);

  // Asked of the graph engine, because the WorkOrder page cannot show a graph
  // run's position yet. The checkout it names is a directory that exists, and
  // the file the agent wrote is in it.
  await expect
    .poll(async () => (await graphRun(page, runUrl)).values?.implementation ?? "", {
      timeout: 60_000,
    })
    .toContain(IMPLEMENTED);
  const run = await graphRun(page, runUrl);
  const workspace = String(run.values?.workspace ?? "");
  expect(run.values?.name).toBe(TITLE);
  expect(existsSync(workspace)).toBe(true);
  expect(existsSync(path.join(workspace, GREETING))).toBe(true);
  await expect(page.getByRole("heading", { name: TITLE, level: 1 })).toBeVisible();
  await shot(page, testInfo, "2 implemented");
});

test("@beta the workflow tools reach the agent in a session it accepts", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);

  // Implementation is the first node to attach the run-bound workflow tools,
  // so a server description ACP would refuse stops the run *here*: naming
  // opens a session with no MCP servers at all and succeeds, and the run then
  // fails with `session/new` refused before the implementation agent has said
  // a word. Polled on both, so a refusal is reported as the message the agent
  // sent rather than as a timeout waiting for work that never started.
  await expect
    .poll(
      async () => {
        const run = await graphRun(page, runUrl);
        return {
          error: String(run.error ?? ""),
          implementation: String(run.values?.implementation ?? ""),
        };
      },
      { timeout: 60_000 },
    )
    .toEqual({ error: "", implementation: expect.stringContaining(IMPLEMENTED) });
});

test("@beta the WorkOrder page shows a graph run's stages", async ({ page, engine }) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  // Naming is an explicit graph stage even though it is hidden from the
  // conversation rail.
  await expect(page.locator(".stages .stage")).toHaveText([
    "Workspace",
    "Naming",
    "Implementation",
    "Review (Security)",
    "Review (Bugs & task adherence)",
    "Review (Performance)",
    "Review (Conciseness)",
    "Reranker",
    "Human review",
  ]);
});

test("@beta the checkout a graph run works in is on its WorkOrder page", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  const checkout = page.locator(".run-workspace .dock-path");
  await expect(checkout).toContainText("cd ");
});

test("@beta the rail offers a graph WorkOrder's conversations by node", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  // The two nodes a person can read, from the moment the run exists: the
  // checkout and the human verdict are stages of the run rather than
  // conversations in it, and say so about themselves.
  const conversations = page.getByLabel(/^Conversations for /);
  await expect(conversations.getByRole("link")).toHaveText([
    "Implementation",
    "Review (Security)",
    "Review (Bugs & task adherence)",
    "Review (Performance)",
    "Review (Conciseness)",
    "Reranker",
  ]);

  await conversations.getByRole("link", { name: "Implementation" }).click();

  await expect(page).toHaveURL(/\/conversations\/graph--implementation$/);
  await expect(page.locator(".rail-sub a[aria-current='page']")).toHaveText(
    "Implementation",
  );
});

/** Open the implementation node's conversation from the WorkOrder page. */
async function openConversation(page: Page, runUrl: string): Promise<void> {
  await page.goto(runUrl);
  await page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name: "Implementation", exact: true }) })
    .getByRole("link", { name: "Open conversation" })
    .click();
  await expect(page).toHaveURL(/\/conversations\//);
}

test("@beta an agent's conversation is readable from the WorkOrder page", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await openConversation(page, runUrl);

  // Both halves of it, drawn as the chat draws a chat: what the node was asked
  // is the reader's turn, what it said is the agent's, and the command it ran
  // is a folded row in between rather than a paragraph of its own.
  await expect(page.locator(".message-user").first()).toContainText(TASK);
  await expect(page.getByText("Writing the greeting.")).toBeVisible();
  await expect(page.locator(".message-assistant .tool").first()).toContainText(
    `echo hello > ${GREETING}`,
  );
  await shot(page, testInfo, "4 the conversation");
});

test("@beta an agent waiting on permission is answered in its conversation", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(ASKING_SCRIPT);

  const runUrl = await create(page, engine.repository);
  await expect
    .poll(async () => (await graphRun(page, runUrl)).pendingApprovals?.length ?? 0, {
      timeout: 60_000,
    })
    .toBe(1);
  await openConversation(page, runUrl);

  // The question, where the command it is about is: the run is stopped here,
  // and this is the page the person it is stopped on is reading.
  const card = page.locator(".approval-pending");
  await expect(card).toContainText(`echo hello > ${GREETING}`);
  await shot(page, testInfo, "5 waiting on permission");

  // Steering, while it waits. The agent is holding its turn, so this is a
  // message into the conversation rather than a new one.
  await page.getByLabel("Message the agent").fill(STEER);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(STEER)).toBeVisible();

  await card.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByText(STEERED)).toBeVisible({ timeout: 60_000 });
});

test("@beta a graph agent responds when steered during an executing turn", async ({
  page,
  engine,
}) => {
  engine.script(STEERING_SCRIPT);

  const runUrl = await create(page, engine.repository, INTERRUPT_TASK);
  const runId = runUrl.split("/").pop() ?? "";
  await expect
    .poll(async () => String((await graphRun(page, runUrl)).values?.workspace ?? ""))
    .not.toBe("");
  const workspace = String((await graphRun(page, runUrl)).values.workspace);

  try {
    await openConversation(page, runUrl);
    await expect(page.getByText(INTERRUPT_WORKING)).toBeVisible();

    await page.getByLabel("Message the agent").fill(INTERRUPT_STEERING);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(INTERRUPT_STEERING)).toBeVisible();
    await expect(page.getByText(ACKNOWLEDGEMENT)).toBeVisible();
  } finally {
    writeFileSync(path.join(workspace, RELEASE), "go", "utf-8");
  }
});

test("@beta a graph run waiting on a person says so, and can be answered", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);

  // The graph engine is the one that knows: the run reaches its human-review
  // node and raises an approval there.
  await expect
    .poll(async () => (await graphRun(page, runUrl)).pendingApprovals?.length ?? 0, {
      timeout: 60_000,
    })
    .toBe(1);
  await page.goto(runUrl);
  await shot(page, testInfo, "3 waiting for a person");

  // What a person is shown, and what they press. Both are what the step
  // WorkOrder does at exactly this point.
  await expect(page.locator(".callout-action")).toContainText("Action required");
  await page.getByLabel("Decision note").fill("Ship it.");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
});
