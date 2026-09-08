import { defineConfig, devices } from "@playwright/test";

/** The browser tier: a real server, real provider protocols, real worktrees.
 *
 *  Each test composes its own application over a scripted CLI (`e2e/harness.ts`),
 *  so there is no shared server here and no `webServer` block -- only the client
 *  build every one of those servers needs to serve.
 *
 *  Tests tagged `@beta` are the `[BETA]` graph WorkOrder's, and are expected to
 *  fail while the interface catches up with the graph engine. They are run by
 *  their own job, which is allowed to be red -- `npm run test:e2e` leaves them
 *  out, and `npm run test:e2e:beta` runs only them. */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/build-client.ts",
  // A turn spawns a CLI, checks a worktree out, and waits on a person.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  // A flaky end-to-end test is a bug report about the product, so a failure is
  // reported rather than retried into a pass.
  retries: 0,
  // The HTML report is how a run is read after the fact, so it is written
  // whether or not anyone is watching the terminal. `open: "never"` keeps it
  // from hijacking a browser at the end of a local run.
  reporter: process.env.CI
    ? [["github"], ["list"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],
  // A trace of two passing tests costs nothing, and the run somebody asks
  // about is more often a green one than the one that happened to fail. CI
  // keeps only failures, because there it is an artifact somebody pays to
  // store.
  use: { trace: process.env.CI ? "retain-on-failure" : "on" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
