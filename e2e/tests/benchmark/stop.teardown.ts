import { test as teardown } from "@playwright/test"
import { stopProcesses } from "../../support/benchmark.ts"

teardown("stop the tunnel and daemons this run started", async () => {
  // Whatever happened, a tunnel or a daemon left running would stay reachable or keep spending.
  // The repository and folder stay for a person to look at; the next run's setup clears them.
  console.log("stopped processes:", await stopProcesses())
})
