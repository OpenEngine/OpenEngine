import { test } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { benchmarkCategory } from "../docCategories"

test.describe(benchmarkCategory, { tag: ["@benchmark"] }, () => {
  test.describe(
    withDocMeta("Build a Calculator With OpenEngine", {
      description: "How to have OpenEngine build a whole project from a list of GitHub issues, step by step.",
      sidebar_position: 1,
      slug: "/start-to-finish/build-a-calculator",
    }),
    () => {
      test("The journey", async () => {
        await test.step("[nodoc] The benchmark runs", async () => {})
      })
    },
  )
})
