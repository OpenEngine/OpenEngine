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
      await expect.poll(() => bufferText(page)).toContain(`${ids[0]} ${ids[1]} ${ids[0]} | ${time}`)
      // The echoed line, not the command that typed it: the buffer can be
      // ahead of the rows drawn on screen, so wait for the row itself.
      const row = page.locator(".xterm-rows > div").filter({ hasText: /^\s*[a-z]+-[0-9a-f]{12} .*\|/ })
      await expect(row).toHaveCount(1)
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

  test("shows the ports of daemons on this machine as stand-ins that are the same on every run", async ({ browser }) => {
    // Each e2e daemon listens on a port the OS picked, and a guide about
    // backends shows their URLs.
    const shown = async (ports: number[]) => {
      const page = await browser.newPage()
      await openTerminal(page)
      await page.locator("#terminal").click()
      const urls = `http://127.0.0.1:${ports[0]} http://localhost:${ports[1]} http://127.0.0.1:${ports[0]}/api`
      await page.keyboard.type(`echo "${urls} |"`)
      await page.keyboard.press("Enter")
      await expect.poll(() => bufferText(page)).toContain(`${urls} |`)
      // The echoed line, not the command that typed it: the buffer can be
      // ahead of the rows drawn on screen, so wait for the row itself.
      const row = page.locator(".xterm-rows > div").filter({ hasText: /^\s*http:.*\|\s*$/ })
      await expect(row).toHaveCount(1)
      const text = (await row.textContent())!.trim()
      await page.close()
      return text
    }
    const first = await shown([53117, 61904])
    const second = await shown([49852, 50331])
    // The same stand-ins whatever the real ports were, the same length so
    // nothing moves, and the same one wherever a port repeats.
    expect(first).toBe(second)
    const [local, other, again] = first.split(" ")
    const [, port] = /^http:\/\/127\.0\.0\.1:(\d{5})$/.exec(local!) ?? []
    expect(port).toBeDefined()
    expect(port).not.toBe("53117")
    expect(other).toMatch(/^http:\/\/localhost:\d{5}$/)
    expect(other!.split(":")[2]).not.toBe(port)
    expect(again).toBe(`${local}/api`)
  })

  test("says so when the server refuses the session", async ({ page }) => {
    await page.goto("/?token=wrong")
    await page.waitForFunction(() => window.__term?.error !== undefined)
    await expect(page.locator(".xterm-rows")).toContainText("could not connect")
  })
})
