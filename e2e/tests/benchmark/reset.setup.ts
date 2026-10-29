import { test as setup } from "@playwright/test"
import { assertTokenScopes, startContainer, teardown } from "../../support/benchmark.ts"

setup("start the benchmark from a clean computer", async () => {
  // Before anything is created or deleted: a missing scope would only show up hours into a run.
  await assertTokenScopes()
  // Delete the previous run's repository and container. Safe on a clean machine.
  console.log("teardown:", await teardown())
  // The computer the journey happens on. Built the first time, then cached by Docker.
  await startContainer()
})
