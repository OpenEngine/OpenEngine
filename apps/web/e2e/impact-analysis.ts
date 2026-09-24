import type { Scenario } from "./harness";

export const IMPACT_ASSESSMENT = "Green 🟢: A narrow, tested change requiring no human setup.";

export const IMPACT_ANALYSIS_SCENARIO: Scenario = {
  when: "Assess the impact of the final change",
  steps: [
    { type: "say", text: IMPACT_ASSESSMENT },
    {
      type: "tool",
      name: "add_comment",
      arguments: {
        pr_url: "https://github.com/acme/repository/pull/7",
        comment: IMPACT_ASSESSMENT,
      },
    },
    {
      type: "tool",
      name: "complete_step",
      arguments: {
        outcome: "success",
        summary: IMPACT_ASSESSMENT,
        outputs: {
          impact_level: "Green",
          impact_rationale: "A narrow, tested change requiring no human setup.",
        },
      },
    },
  ],
};
