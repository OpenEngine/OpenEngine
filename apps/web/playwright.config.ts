import { defineConfig, devices } from "@playwright/test";

/** The browser tier: a real server, real provider protocols, real worktrees.
 *
 *  Each test composes its own application over a scripted CLI (`e2e/harness.ts`),
 *  so there is no shared server here and no `webServer` block -- only the client
 *  build every one of those servers needs to serve. */
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
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: { trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
