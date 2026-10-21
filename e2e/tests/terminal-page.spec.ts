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

  test("shows IDs and timestamps as stand-ins that are the same on every run", async ({ browser }) => {
    const shown = async (ids: string[], time: string) => {
      const page = await browser.newPage()
      await openTerminal(page)
      await page.locator("#terminal").click()
      await page.keyboard.type(`echo "${ids[0]} ${ids[1]} ${ids[0]} | ${time}"`)
      await page.keyboard.press("Enter")
      // The echoed command contains the same text; wait for the output row.
      await expect.poll(async () => (await bufferText(page)).split("\n")).toContain(`${ids[0]} ${ids[1]} ${ids[0]} | ${time}`)
      const row = page.locator(".xterm-rows > div").filter({ hasText: /^g-/ }).last()
      await expect(row).toHaveText(/^g-[0-9a-f]{12} run-[0-9a-f]{12} g-[0-9a-f]{12} \| \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$/)
      const text = (await row.textContent())!.trim()
      await page.close()
      return text
    }
    const first = await shown(["g-0123456789ab", "run-aaaaaaaaaaaa"], "2026-10-12T23:04:01+00:00")
    const second = await shown(["g-ffffffffffff", "run-bbbbbbbbbbbb"], "2027-03-01T08:15:59+00:00")
    // The same stand-ins whatever the real values were, the same length so
    // nothing moves, and the same one wherever an ID repeats.
    expect(first).toBe(second)
    const [id, run, again, , time] = first.split(" ")
    expect(id).toMatch(/^g-[0-9a-f]{12}$/)
    expect(run).toMatch(/^run-[0-9a-f]{12}$/)
    expect(again).toBe(id)
    expect(time).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$/)
    expect(id).not.toBe("g-0123456789ab")
  })

  test("shows a port the OS picked on this computer as a stand-in that is the same on every run", async ({ browser }) => {
    const shown = async (port: number) => {
      const page = await browser.newPage()
      await openTerminal(page)
      await page.locator("#terminal").click()
      await page.keyboard.type(`echo "local http://127.0.0.1:${port} | mac-mini http://mac-mini.local:4364"`)
      await page.keyboard.press("Enter")
      await expect.poll(() => bufferText(page)).toMatch(new RegExp(`^local http://127\\.0\\.0\\.1:${port} \\|`, "m"))
      const row = page.locator(".xterm-rows > div").filter({ hasText: /^local http/ }).last()
      const text = (await row.textContent())!.trim()
      await page.close()
      return text
    }
    const first = await shown(51234)
    const second = await shown(60789)
    // The same stand-in whatever the real port was, the same length so nothing
    // moves; a port someone chose, like the Mac mini's, shows as it is.
    expect(first).toBe(second)
    expect(first).toMatch(/^local http:\/\/127\.0\.0\.1:\d{5} \| mac-mini http:\/\/mac-mini\.local:4364$/)
    expect(first).not.toContain("51234")
  })

  test("says so when the server refuses the session", async ({ page }) => {
    await page.goto("/?token=wrong")
    await page.waitForFunction(() => window.__term?.error !== undefined)
    await expect(page.locator(".xterm-rows")).toContainText("could not connect")
  })
})
