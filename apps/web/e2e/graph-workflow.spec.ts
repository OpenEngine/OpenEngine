/** The WorkOrder this repository ships, end to end, run by the graph engine.
 *
 *  Split into the states a run passes through, one test each, rather than
 *  written as one long journey: a graph run reaches each of them for its own
 *  reasons, and a spec per state reports every one that broke instead of only
 *  the first. It began as the split half of `workflow-run.spec.ts`, which
 *  covered the step workflow this repository no longer ships; what that spec
 *  was the only cover for -- the reviewer's finding leaving through `gh`, and
 *  the finished run surviving a reload -- is in the last test here.
 *
 *  Everything here is the real thing except the model: a real server, the real
 *  graph engine, real LangGraph, real checkpoint files, a real worktree, and
 *  agents reached over real ACP -- answered by `tests/provider_fakes.py`
 *  instead of by codex or claude. */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, shot, test, type Script } from "./harness";

/** What the workflow dropdown calls the graph. */
const WORKFLOW = "Implementation review rerank";
const TASK = "Add a greeting file to the repository.";
const TITLE = "Adding a greeting";
const NAMING_REQUEST = "Give this WorkOrder a concise display name";
const GREETING = "greeting.txt";
const PULL_REQUEST = "https://github.com/acme/repository/pull/7";
const IMPLEMENTED = "Wrote the greeting.";
const REVIEWED = "Read the change; greeting.txt is not covered by a test.";
const DECISION = "The finding can wait; ship the greeting.";

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

/** Create one graph WorkOrder and land on its page. */
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

test("a graph workflow accepts independent stage runners", async ({
  page,
  engine,
}, testInfo) => {
  engine.script(SCRIPT);

  await page.goto("/runs/new");

  await expect(
    page.getByLabel("Workflow definition").getByRole("option", { name: WORKFLOW }),
  ).toHaveCount(1);
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  const implementation = page.getByLabel("Implementation runner");
  const review = page.getByLabel("Review runner");
  await expect(implementation).toHaveValue("codex");
  await expect(review).toHaveValue("claude");
  await implementation.selectOption("claude");
  await expect(review).toHaveValue("claude");
  await review.selectOption("codex");
  await expect(implementation).toHaveValue("claude");
  await page.getByText("Workflow inputs", { exact: true }).click();
  await expect(implementation).toBeHidden();
  await expect(review).toBeHidden();
  await page.getByText("Workflow inputs", { exact: true }).click();
  await expect(implementation).toHaveValue("claude");
  await expect(review).toHaveValue("codex");
  await shot(page, testInfo, "1 the beta choice");

  await page.getByLabel("Repository").fill(engine.repository);
  await page.getByLabel("Task prompt").fill(TASK);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  const runUrl = new URL(page.url()).pathname;
  await expect.poll(async () => (await graphRun(page, runUrl)).values?.review, {
    timeout: 60_000,
  }).toEqual([]);
  const run = await graphRun(page, runUrl);
  expect(run.values.inputs).toEqual({
    implementation_runner: "claude",
    review_runner: "codex",
  });
  await openConversation(page, runUrl);
  await expect(page.getByLabel("Runner", { exact: true })).toHaveValue("claude");
  await page.getByLabel("Runner", { exact: true }).selectOption("codex");
  await expect.poll(async () =>
    (await graphRun(page, runUrl)).runnerOverrides?.implementation ?? "codex"
  ).toBe("codex");
  await page.reload();
  await expect(page.getByLabel("Runner", { exact: true })).toHaveValue("codex");
});

test("a graph WorkOrder provisions a checkout and runs its agents", async ({
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

test("the workflow tools reach the agent in a session it accepts", async ({
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

test("the WorkOrder page shows a graph run's stages", async ({ page, engine }) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  // Naming is an explicit graph stage even though it is hidden from the
  // conversation rail.
  await expect(page.locator(".stages .stage")).toHaveText([
    "Workspace",
    "Naming",
    "Implementation",
    "Review",
    "Reranker",
    "Human review",
  ]);
});

test("the checkout a graph run works in is on its WorkOrder page", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  const checkout = page.locator(".run-workspace .dock-path");
  await expect(checkout).toContainText("cd ");
});

test("the rail offers a graph WorkOrder's conversations by node", async ({
  page,
  engine,
}) => {
  engine.script(SCRIPT);

  const runUrl = await create(page, engine.repository);
  await page.goto(runUrl);

  // The nodes a person can read, from the moment the run exists: the
  // checkout and the human verdict are stages of the run rather than
  // conversations in it, and say so about themselves. The review agents are
  // available under their shared group rather than filling the rail at once.
  const conversations = page.getByLabel(/^Conversations for /);
  await expect(conversations.getByRole("link")).toHaveText([
    "Implementation",
    "Reranker",
  ]);
  await conversations.getByText("Review", { exact: true }).click();
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

test("an agent's conversation is readable from the WorkOrder page", async ({
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

test("an agent waiting on permission is answered in its conversation", async ({
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

test("a graph agent responds when steered during an executing turn", async ({
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

/** One step's card on the run page, by the name the read model gives it.
 *
 *  Exactly, because "Review" is also how "Human review" starts. */
function step(page: Page, name: string) {
  return page
    .locator(".step")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

test("a graph run waiting on a person says so, and can be answered", async ({
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

  // What each node reported, on the page rather than only in the graph state.
  // Its *summary*, not the outputs it declared: a graph run's page does not
  // show those yet -- see the note in `README.md` -- and a step run's did.
  await expect(step(page, "Implementation")).toContainText(IMPLEMENTED);

  // The reranker's comment left the process the way a real one would, through
  // `gh` -- which here records rather than commenting on somebody's repository.
  expect(readFileSync(engine.ghLog, "utf-8")).toContain(REVIEWED);

  // What a person is shown, and what they press. Pressing one of these is the
  // only thing in the browser that can end a run.
  await expect(page.locator(".callout-action")).toContainText("Action required");
  await page.getByLabel("Decision note").fill(DECISION);
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
  await shot(page, testInfo, "6 approved");

  // Reload rather than re-render: the end state is the store's, so it has to
  // survive the page that submitted it going away.
  await page.reload();
  await expect(page.locator(".detail-title .chip")).toHaveText("succeeded");
  await expect(page.locator(".stats")).toContainText("succeeded");
  const stages = page.locator(".stages .stage");
  await expect(stages).toHaveText([
    "Workspace",
    "Naming",
    "Implementation",
    "Review",
    "Reranker",
    "Human review",
  ]);
  for (const index of [0, 1, 2, 3, 4, 5])
    await expect(stages.nth(index)).toHaveAttribute("data-status", "completed");
  await expect(page.locator(".callout-action")).toHaveCount(0);
});
