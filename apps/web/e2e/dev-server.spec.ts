/** The interface as `engine-dev` serves it: Vite in front, the API behind.
 *
 *  Every other spec here opens the client the API serves out of `dist/`, where
 *  one origin answers for everything and no request can be addressed to a
 *  server that was never told about it. The dev server is not that: it forwards
 *  the prefixes `vite.config.ts` names and answers everything else with
 *  `index.html` and a 200, so a client request to an unforwarded path does not
 *  arrive as a 404 -- it arrives as a page, and `response.json()` says
 *  "unexpected character at line 1 column 1" instead.
 *
 *  Which is a state no other spec in this directory can reach, and is why the
 *  `[BETA]` WorkOrder below was broken in development while all six `@beta`
 *  tests were green: its page reads the graph engine under `/graph`, and only
 *  `/api` was proxied.
 *
 *  Deliberately not tagged `@beta`. The `@beta` job is allowed to be red and is
 *  left out of `npm run test:e2e`; this is a bug in the dev server rather than
 *  a gap in the graph interface, and it has to be a test that goes red for
 *  everyone when the next prefix is added on one side only. */

import { expect, shot, test, type Script } from "./harness";

const WORKFLOW = "[BETA] Implementation review (codex)";
const TASK = "Add a greeting file to the repository.";

const SCRIPT: Script = {
  title: "Adding a greeting",
  scenarios: [
    { when: "Review the implementation", steps: [{ type: "say", text: "Read it." }] },
    {
      when: "Implement the requested change",
      steps: [{ type: "say", text: "Wrote the greeting." }],
    },
  ],
};

test("a WorkOrder page reaches every server it reads, through the dev proxy", async ({
  page,
  engine,
  devServer,
}, testInfo) => {
  // Two servers to start rather than one, and Vite compiles the client on the
  // first request instead of being handed a build.
  test.slow();
  engine.script(SCRIPT);

  await page.goto(`${devServer}/runs/new`);
  await page.getByLabel("Workflow definition").selectOption({ label: WORKFLOW });
  await page.getByLabel("Repository").fill(engine.repository);
  await page.getByLabel("Task prompt").fill(TASK);
  await page.getByRole("button", { name: "Create WorkOrder" }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);

  // The stages come from the graph engine's own topology, under `/graph`, so
  // drawing them at all is the assertion: an unforwarded prefix leaves the page
  // reporting `Unexpected token '<'` where the run should be.
  await expect(page.locator(".stages .stage")).toHaveText([
    "Workspace",
    "Implementation",
    "Review",
    "Human review",
  ]);
  // Afterwards, because the page polls: a prefix forwarded to the wrong place
  // would draw the run once and then report the parse error over the top of it.
  await expect(page.getByText("Could not load WorkOrder")).toHaveCount(0);
  await shot(page, testInfo, "1 the WorkOrder, served by the dev server");
});
