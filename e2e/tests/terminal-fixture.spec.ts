import { execFileSync } from "node:child_process"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect } from "../fixtures/terminal.ts"
import { engineBin } from "../support/engine"

test.describe("terminal fixture", { tag: ["@skip-docs"] }, () => {
  test("runs a command and returns its output and exit code", async ({ terminal }) => {
    const result = await terminal.run("echo alpha; echo beta")
    expect(result).toEqual({ output: "alpha\nbeta", exitCode: 0 })
  })

  test("fails a command that exits differently than expected, unless told to expect it", async ({ terminal }) => {
    await expect(terminal.run("false")).rejects.toThrow(/exited with 1/)
    expect((await terminal.run("sh -c 'exit 3'", { exitCode: 3 })).exitCode).toBe(3)
  })

  test("runs this checkout's engine, whatever else is installed", async ({ terminal }) => {
    const version = execFileSync(engineBin, ["--version"], { encoding: "utf8" }).trim()
    expect((await terminal.run("oe --version")).output).toBe(version)
  })

  test("keeps long output lines whole even when the terminal wraps them", async ({ terminal }) => {
    const long = "x".repeat(250)
    expect((await terminal.run(`echo ${long}`)).output).toBe(long)
  })

  test("types, presses keys and interrupts with Control+C", async ({ terminal }) => {
    // Interrupt once the command has the terminal, as a person would: a ^C in
    // the instant after Enter reaches the shell, not the command.
    await terminal.type("sh -c 'echo started; exec sleep 30'")
    await terminal.press("Enter")
    await terminal.waitForText(/^started$/m)
    await terminal.press("Control+C")
    expect(await terminal.waitForPrompt()).toBe(130)
  })

  test("waits for text to appear and for the screen to settle", async ({ terminal }) => {
    await terminal.type("sh -c 'for i in 1 2 3; do echo tick-$i; sleep 0.2; done'")
    await terminal.press("Enter")
    await terminal.waitForText(/^tick-3$/m)
    await terminal.waitForQuiet(300)
    expect(await terminal.text()).toMatch(/tick-1\ntick-2\ntick-3/)
  })

  test("waits for a session's command to exit", async ({ page }) => {
    const { Terminal } = await import("../fixtures/terminal.ts")
    const terminal = await Terminal.open(page, { command: "/bin/sh", args: ["-c", "echo done; exit 4"] })
    expect(await terminal.waitForExit()).toBe(4)
    expect(await terminal.text()).toContain("done")
  })

  test("sizes the page to the terminal so screenshots are the terminal and nothing else", async ({ page, terminal }) => {
    const box = await terminal.screen().boundingBox()
    expect(page.viewportSize()).toEqual({ width: Math.ceil(box!.width), height: Math.ceil(box!.height) })
  })

  test("crops a screenshot to the rows in use", async ({ terminal }) => {
    await terminal.run("echo one; echo two")
    const area = await terminal.usedArea()
    const screen = (await terminal.screen().boundingBox())!
    const row = (await (await terminal.row(/^two$/)).boundingBox())!
    expect(area.width).toBe(screen.width)
    // The command, its two lines and the new prompt, plus the frame's padding.
    expect(area.height).toBeGreaterThan(row.y + row.height * 2)
    expect(area.height).toBeLessThan(row.y + row.height * 2 + 30)
  })

  test("locates the text on one row, hugging it rather than the whole line", async ({ terminal }) => {
    await terminal.run("echo needle")
    const row = await terminal.row(/^needle$/)
    const rowBox = (await row.boundingBox())!
    const screenBox = (await terminal.screen().boundingBox())!
    await expect(row).toBeVisible()
    expect(rowBox.width).toBeLessThan(screenBox.width / 4)
    expect(rowBox.height).toBeGreaterThan(10)
    expect(rowBox.height).toBeLessThan(30)
    expect(await terminal.textAt(row)).toBe("needle")
  })

  test("locates a range of rows as one element", async ({ terminal }) => {
    await terminal.run("echo top; echo middle; echo bottom")
    const one = (await (await terminal.row(/^top$/)).boundingBox())!
    const range = await terminal.rows(/^top$/, /^bottom$/)
    const box = (await range.boundingBox())!
    expect(box.height).toBeCloseTo(one.height * 3, 0)
    expect(await terminal.textAt(range)).toBe("top\nmiddle\nbottom")
  })

  test("refuses to locate text that isn't on screen", async ({ terminal }) => {
    await expect(terminal.row("not there", { timeout: 500 })).rejects.toThrow(/not there/)
  })

  test("annotates a row in a test2doc screenshot", async ({ terminal }, testInfo) => {
    await terminal.run("echo annotated")
    await screenshot(testInfo, await terminal.row(/^annotated$/), { annotation: { text: "This row" } })
    expect(testInfo.attachments.some((attachment) => attachment.name.startsWith("test2doc-"))).toBe(true)
  })
})
