import { test, expect, type Page } from "@playwright/test"
import { engineBin } from "../support/engine"
import "../terminal/page/api.ts"

type Session = { cmd?: string; args?: string[]; cwd?: string; env?: Record<string, string>; cols?: number; rows?: number }

/** Open the terminal page on a new session and wait until its PTY has started. */
async function openTerminal(page: Page, session: Session = {}) {
  const params = new URLSearchParams({ token: process.env.E2E_TERMINAL_TOKEN! })
  if (session.cmd) params.set("cmd", session.cmd)
  for (const arg of session.args ?? []) params.append("arg", arg)
  if (session.cwd) params.set("cwd", session.cwd)
  if (session.env) params.set("env", JSON.stringify(session.env))
  if (session.cols) params.set("cols", String(session.cols))
  if (session.rows) params.set("rows", String(session.rows))
  await page.goto(`/?${params}`)
  await page.waitForFunction(() => window.__term?.ready === true)
}

const bufferText = (page: Page) => page.evaluate(() => window.__term!.text())

test.describe("terminal page", { tag: ["@skip-docs"] }, () => {
  test("renders the terminal with the DOM renderer, one element per row", async ({ page }) => {
    await openTerminal(page, { cols: 80, rows: 24 })
    await expect(page.locator(".xterm-rows > div")).toHaveCount(24)
    await expect(page.locator(".xterm-rows")).toContainText("$")
    await expect(page.locator("canvas")).toHaveCount(0)
  })

  test("sends keystrokes to the shell and exposes the buffer as text", async ({ page }) => {
    await openTerminal(page)
    await page.locator("#terminal").click()
    await page.keyboard.type("echo page-$((6 * 7))")
    await page.keyboard.press("Enter")
    await expect.poll(() => bufferText(page)).toMatch(/^page-42$/m)
  })

  test("finds the row a piece of text is on", async ({ page }) => {
    await openTerminal(page)
    await page.locator("#terminal").click()
    await page.keyboard.type("echo first; echo second")
    await page.keyboard.press("Enter")
    await expect.poll(() => bufferText(page)).toMatch(/^second$/m)
    const row = await page.evaluate(() => window.__term!.rowIndexOf({ source: "^second$" }))
    expect(row).toBeGreaterThan(0)
    await expect(page.locator(".xterm-rows > div").nth(row)).toHaveText("second")
    expect(await page.evaluate(() => window.__term!.rowIndexOf("nowhere to be found"))).toBe(-1)
  })

  test("runs the requested command with its directory and environment", async ({ page }) => {
    await openTerminal(page, {
      cmd: "/bin/sh",
      args: ["-c", 'echo "$GREETING from $(pwd)"'],
      cwd: "/tmp",
      env: { GREETING: "hello" },
    })
    await expect.poll(() => bufferText(page)).toMatch(/hello from (\/private)?\/tmp/)
  })

  test("reports when the command exits, and its exit code", async ({ page }) => {
    await openTerminal(page, { cmd: "/bin/sh", args: ["-c", "exit 2"] })
    await page.waitForFunction(() => window.__term!.exited)
    expect(await page.evaluate(() => window.__term!.exitCode)).toBe(2)
  })

  test("counts output as it arrives so a test can wait for it to settle", async ({ page }) => {
    await openTerminal(page)
    const before = await page.evaluate(() => window.__term!.outputCount)
    await page.locator("#terminal").click()
    await page.keyboard.type("echo tick")
    await page.keyboard.press("Enter")
    await expect.poll(() => page.evaluate(() => window.__term!.outputCount)).toBeGreaterThan(before)
    expect(await page.evaluate(() => window.__term!.lastOutputAt)).toBeGreaterThan(0)
  })

  test("draws engine --help legibly in the bundled font, the same way every time", async ({ page }) => {
    await openTerminal(page, { cmd: engineBin, args: ["--help"], cols: 100, rows: 48 })
    await page.waitForFunction(() => window.__term!.exited)
    expect(await page.evaluate(() => document.fonts.check('14px "JetBrains Mono"'))).toBe(true)
    await expect(page.locator(".xterm-rows")).toContainText("usage: engine")
    const frame = page.locator("#terminal")
    const first = await frame.screenshot()
    const second = await frame.screenshot()
    expect(first.equals(second)).toBe(true)
  })

  test("says so when the server refuses the session", async ({ page }) => {
    await page.goto("/?token=wrong")
    await page.waitForFunction(() => window.__term?.error !== undefined)
    await expect(page.locator(".xterm-rows")).toContainText("could not connect")
  })
})
