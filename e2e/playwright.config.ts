import { randomBytes } from "node:crypto"
import { defineConfig, devices } from "@playwright/test"
import { freePort } from "./support/freePort.ts"

// Chosen once in the runner and inherited by its workers, which load this
// config again and must agree on where the terminal server is.
process.env.E2E_TERMINAL_PORT ??= String(freePort())
// The terminal server's session token. The server reads it from its inherited
// environment and the specs pass it to the page, so it never appears in argv.
process.env.E2E_TERMINAL_TOKEN ??= randomBytes(24).toString("base64url")
const terminalUrl = `http://127.0.0.1:${process.env.E2E_TERMINAL_PORT}`

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
  baseURL: terminalUrl,
  trace: "on-first-retry",
} as const

/** The terminal bridge (terminal/server.ts) the specs drive the CLI through. */
export const webServerConfig = {
  command: `npm run build:terminal && node terminal/server.ts --port ${process.env.E2E_TERMINAL_PORT} --static terminal/dist`,
  url: `${terminalUrl}/healthz`,
  reuseExistingServer: false,
}

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
  webServer: webServerConfig,
})
