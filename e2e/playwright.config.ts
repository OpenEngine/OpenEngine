import { defineConfig, devices } from "@playwright/test"

/**
 * Settings the test run and playwright-test2doc.config.ts must share, exported
 * rather than copied so the doc pass can't silently drift from the tests.
 */
export const projects = [
  {
    name: "cli",
    testDir: "./tests",
    use: { ...devices["Desktop Chrome"] },
  },
]

export const expectConfig = {
  timeout: 10_000,
}

export const sharedUse = {
  trace: "on-first-retry",
} as const

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  expect: expectConfig,
  use: sharedUse,
  projects,
})
