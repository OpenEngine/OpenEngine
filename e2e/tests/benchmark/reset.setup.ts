import { test as setup } from "@playwright/test"
import { teardown } from "../../support/benchmark.ts"

setup("clear what the last benchmark run left", async () => {
  // Delete the previous run's repository, folder and processes. Safe on a clean machine.
  console.log("teardown:", await teardown())
})
