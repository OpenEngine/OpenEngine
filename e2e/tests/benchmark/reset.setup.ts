import { test as setup } from "@playwright/test"
import { startContainer, teardown } from "../../support/benchmark.ts"

setup("start the benchmark from a clean computer", async () => {
  // Delete the previous run's repository and container. Safe on a clean machine.
  console.log("teardown:", await teardown())
  // The computer the journey happens on. Built the first time, then cached by Docker.
  await startContainer()
})
