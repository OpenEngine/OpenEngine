import { mkdirSync } from "node:fs"
import { defineConfig } from "@playwright/test"
import { projects, expectConfig, sharedUse, webServerConfig } from "./playwright.config.ts"
import "@test2doc/playwright/types"

// The benchmark guide has its own directory, beside the site's other docs, so a noisy
// run can't churn the committed guides in site/docs/guides. test2doc cleans only the
// pages it wrote, so the hand-written index.md here survives.
const outputDir = "../site/docs/benchmark"
mkdirSync(outputDir, { recursive: true })

export default defineConfig({
  testDir: "./tests",
  // A benchmark run takes as long as it takes; the harness's own dollar and wall-clock cap stops a
  // stuck one. Waits on GitHub, CI or the deploy poll with their own bounded deadlines.
  timeout: 0,
  expect: expectConfig,
  use: {
    ...sharedUse,
    test2doc: {
      annotationDefaults: {
        showArrow: true,
        highlightFillStyle: "rgba(0, 0, 0, 0)",
        highlightStrokeStyle: "rgba(255, 0, 0, 0.8)",
        labelBoxFillStyle: "rgba(0, 0, 0, 0.8)",
        labelBoxStrokeStyle: "rgba(255, 0, 0, 1)",
        fillStyle: "rgba(255, 255, 255, 1)",
      },
    },
  },
  reporter: [["list"], ["@test2doc/playwright", { outputDir }]],
  // One worker, in order: the run owns one GitHub account's repository.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  projects: [
    // Clears what the last run left (repo, folder, tunnel) before this one starts. Nothing is
    // deleted afterwards, so a failed run leaves its repo and folder to look at; but its
    // "stop" teardown always stops the tunnel and daemons it started, pass or fail.
    { ...projects[0]!, name: "setup", testDir: "./tests/benchmark", testMatch: /.*\.setup\.ts/, teardown: "stop" },
    { ...projects[0]!, name: "stop", testDir: "./tests/benchmark", testMatch: /.*\.teardown\.ts/ },
    { ...projects[0]!, name: "benchmark", grep: /@benchmark/, dependencies: ["setup"] },
  ],
  webServer: webServerConfig,
})
