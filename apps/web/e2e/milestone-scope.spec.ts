import { readFileSync } from "node:fs";

import { MILESTONE_SCOPING_PROMPT } from "../src/milestone-scope";
import { expect, shot, test } from "./harness";

const MILESTONE_NAME = "Planning foundation";
const MILESTONE_DESCRIPTION =
  "Build the shared project model before implementation work begins.";

test("a milestone scope chat runs the ACP scoper and draws its plan", async ({
  page,
  engine,
}, testInfo) => {
  engine.script({
    title: "Engine roadmap",
    scenarios: [
      {
        steps: [
          {
            type: "tool",
            name: "add_milestone",
            arguments: {
              name: MILESTONE_NAME,
              description: MILESTONE_DESCRIPTION,
            },
          },
          { type: "say", text: "The foundation milestone is ready." },
        ],
      },
    ],
  });

  await page.goto("/plan");
  await page.getByLabel("Message the agent").fill("Plan the foundation milestone.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("The foundation milestone is ready.")).toBeVisible();

  const { projects } = await (await page.request.get("/api/projects")).json();
  const projectId = projects[0].projectId as string;
  const milestoneResponse = await page.request.get(
    `/api/projects/${projectId}/milestones`,
  );
  const milestone = (await milestoneResponse.json()).milestones[0] as {
    milestoneId: string;
  };

  engine.scopingPlan({
    create: [
      {
        milestone_id: milestone.milestoneId,
        name: "Persist work orders",
        objective: "Add durable work-order storage and its API.",
        evidence_requirements: ["Exercise the API from a browser test"],
        dependencies: [],
      },
    ],
    cancel: ["workorder-obsolete"],
    supersede: [
      {
        workorder_id: "workorder-too-large",
        replacements: [
          {
            milestone_id: milestone.milestoneId,
            name: "Render scoping graph",
            objective: "Draw create, cancel, and supersede operations.",
            evidence_requirements: [],
            dependencies: [],
          },
        ],
      },
    ],
    reasons: ["The original work order crosses storage and presentation boundaries."],
  });

  await page.goto(
    `/projects/${projectId}/milestones/${milestone.milestoneId}`,
  );
  await page.getByRole("link", { name: "Scope" }).click();

  await expect(page).toHaveURL(
    `/projects/${projectId}/milestones/${milestone.milestoneId}/scope`,
  );
  await expect(
    page.getByRole("heading", { name: `Scope ${MILESTONE_NAME}`, level: 1 }),
  ).toBeVisible();
  await expect(page.getByLabel("Message the scoper")).toHaveValue(
    MILESTONE_SCOPING_PROMPT,
  );
  await expect(page.getByText(milestone.milestoneId, { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Send" }).click();

  const plan = page.getByRole("region", { name: "Scoping plan" });
  await expect(plan).toBeVisible();
  await expect(plan.getByRole("heading", { name: "Create · 1" })).toBeVisible();
  await expect(plan.getByRole("heading", { name: "Persist work orders" })).toBeVisible();
  await expect(plan.getByText("workorder-obsolete", { exact: true })).toBeVisible();
  await expect(plan.getByText("Replace workorder-too-large", { exact: true })).toBeVisible();
  await expect(plan.getByRole("heading", { name: "Render scoping graph" })).toBeVisible();
  await shot(page, testInfo, "the milestone scoper returns a graphical plan");

  const requests = readFileSync(engine.scoperLog, "utf-8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  const prompt = requests.find(
    (request) => request.method === "session/prompt",
  ).params.prompt[0].text as string;
  const inputs = JSON.parse(prompt.split("Inputs:\n", 2)[1]);

  expect(inputs.milestones).toEqual([
    {
      milestone_id: milestone.milestoneId,
      requirements: [MILESTONE_DESCRIPTION],
      evidence_requirements: [],
      dependencies: [],
      name: MILESTONE_NAME,
    },
  ]);
  expect(inputs.policy.rules).toEqual([MILESTONE_SCOPING_PROMPT]);
});
