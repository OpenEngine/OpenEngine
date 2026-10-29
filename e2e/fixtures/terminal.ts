// Driving a real terminal from a spec: `terminal` is an interactive shell on
// the terminal page, and `Terminal.open` starts any other command there.
//
//   const { output } = await terminal.run("oe graphs list")
//   await screenshot(testInfo, await terminal.row(/fix-flaky-test/), { annotation: { text: "..." } })

import { test as base, expect, type Locator, type Page } from "@playwright/test"
import type { StartMessage } from "../terminal/protocol.ts"
import type { Pattern } from "../terminal/page/api.ts"
import { engineBinDir } from "../support/engine.ts"
import "../terminal/page/api.ts"

export { expect }

export type TerminalSession = Omit<StartMessage, "type">

type Text = string | RegExp

const DEFAULT_TIMEOUT = 30_000

function pattern(text: Text): Pattern {
  return typeof text === "string" ? text : { source: text.source, flags: text.flags }
}

/** Room below a row for test2doc's label: its arrow's 24px margin, a 14px line and 4px padding each side, and some to spare. */
export const LABEL_ROOM = 48

export class Terminal {
  readonly page: Page
  /** The prompt count when Enter was last pressed: the command it submitted is done at the next one. */
  private promptsAtEnter = 0

  private constructor(page: Page) {
    this.page = page
  }

  /**
   * Open the terminal page on a new session, and size the page to the terminal.
   * `oe` on the session's PATH is this checkout's, unless `checkoutEngine` is false: a session
   * that installs OpenEngine itself (the benchmark) runs the one it installed.
   *
   * A session waits for its first prompt before it is handed over, so what a test types is never typed ahead of the
   * shell. That is every session without a `command`; one whose command starts a shell (`docker exec ... bash`) says
   * so with `shell`.
   */
  static async open(
    page: Page,
    session: TerminalSession = {},
    { checkoutEngine = true, shell = !session.command }: { checkoutEngine?: boolean; shell?: boolean } = {},
  ): Promise<Terminal> {
    if (checkoutEngine) {
      session = {
        ...session,
        env: { ...session.env, PATH: `${engineBinDir()}:${session.env?.PATH ?? process.env.PATH}` },
      }
    }
    const params = new URLSearchParams({ token: process.env.E2E_TERMINAL_TOKEN ?? "" })
    if (session.command) params.set("cmd", session.command)
    for (const arg of session.args ?? []) params.append("arg", arg)
    if (session.cwd) params.set("cwd", session.cwd)
    if (session.env) params.set("env", JSON.stringify(session.env))
    if (session.cols) params.set("cols", String(session.cols))
    if (session.rows) params.set("rows", String(session.rows))
    await page.goto(`/?${params}`)
    await page.waitForFunction(() => window.__term?.ready || window.__term?.error !== undefined)
    const error = await page.evaluate(() => window.__term!.error)
    if (error) throw new Error(`terminal session did not start: ${error}`)

    const terminal = new Terminal(page)
    // A page the size of the terminal makes every screenshot, annotated or not,
    // the terminal and nothing else.
    const box = (await terminal.screen().boundingBox())!
    await page.setViewportSize({ width: Math.ceil(box.width), height: Math.ceil(box.height) })
    if (shell) await terminal.waitForPromptCount(1)
    await page.locator(".xterm-helper-textarea").focus()
    return terminal
  }

  /** The terminal, for a screenshot of the whole screen. */
  screen(): Locator {
    return this.page.locator("#terminal")
  }

  /**
   * The terminal down to the last row in use, as a screenshot `clip`, so a
   * short output isn't followed by a screen of empty rows:
   * `screenshot(testInfo, locator, { clip: await terminal.usedArea() })`.
   *
   * An annotation's label goes below its row when the row is above the middle
   * of the page, which for a short output is past the last row in use; with
   * `labelBelow`, the area keeps room for it.
   */
  async usedArea({ labelBelow = false } = {}): Promise<{ x: number; y: number; width: number; height: number }> {
    const area = await this.page.evaluate(() => window.__term!.usedArea())
    return labelBelow ? { ...area, height: area.height + LABEL_ROOM } : area
  }

  /** The whole buffer as text, wrapped lines joined. */
  text(): Promise<string> {
    return this.page.evaluate(() => window.__term!.text())
  }

  async type(text: string) {
    await this.page.keyboard.type(text)
  }

  /**
   * A key or chord, as Playwright names them: "Enter", "Control+C", "ArrowUp".
   * As in a real terminal, a Control+C sent the instant after Enter can reach
   * the shell before the command has started; wait for the command's output
   * (`waitForText`) before interrupting it.
   */
  async press(key: string) {
    if (key === "Enter") this.promptsAtEnter = await this.page.evaluate(() => window.__term!.promptCount)
    await this.page.keyboard.press(key)
  }

  /**
   * Type a command into the shell, run it and wait for the next prompt. Fails
   * unless it exits with `exitCode` (0 unless given).
   */
  async run(command: string, { exitCode = 0, timeout = DEFAULT_TIMEOUT } = {}) {
    const linesBefore = (await this.text()).split("\n").length
    await this.type(command)
    await this.press("Enter")
    const actual = await this.waitForPrompt({ timeout })
    // From the line after the command to the line before the new prompt.
    const output = (await this.text()).split("\n").slice(linesBefore, -1).join("\n")
    if (actual !== exitCode) {
      throw new Error(`\`${command}\` exited with ${actual}, expected ${exitCode}:\n${output}`)
    }
    return { output, exitCode: actual }
  }

  /**
   * Wait for the command submitted by the last Enter to finish, however it
   * ends (including a Control+C), and return its exit code.
   */
  async waitForPrompt({ timeout = DEFAULT_TIMEOUT } = {}) {
    return this.waitForPromptCount(this.promptsAtEnter + 1, timeout)
  }

  private async waitForPromptCount(count: number, timeout = DEFAULT_TIMEOUT) {
    await this.page.waitForFunction((count) => window.__term!.promptCount >= count, count, { timeout })
    return (await this.page.evaluate(() => window.__term!.lastExitCode))!
  }

  /** Wait until `text` is somewhere in the buffer. */
  async waitForText(text: Text, { timeout = DEFAULT_TIMEOUT } = {}) {
    await expect.poll(() => this.text(), { timeout, message: `waiting for ${text} in the terminal` }).toMatch(
      typeof text === "string" ? new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) : text,
    )
  }

  /** Wait until nothing has been drawn for `quietMs`, so a screenshot doesn't catch a frame mid-draw. */
  async waitForQuiet(quietMs = 500, { timeout = DEFAULT_TIMEOUT } = {}) {
    await this.page.waitForFunction(
      (quietMs) => performance.now() - window.__term!.lastOutputAt >= quietMs,
      quietMs,
      { timeout, polling: 50 },
    )
  }

  /** Wait for the session's command to exit (a session opened with `command`), and return its exit code. */
  async waitForExit({ timeout = DEFAULT_TIMEOUT } = {}) {
    await this.page.waitForFunction(() => window.__term!.exited, undefined, { timeout })
    return (await this.page.evaluate(() => window.__term!.exitCode))!
  }

  /** The text on the last visible row matching `text`, as one element hugging it, for a screenshot to highlight. */
  async row(text: Text, options: { timeout?: number } = {}): Promise<Locator> {
    const index = await this.rowIndex(text, options)
    return this.mark(index, index)
  }

  /** The rows from the last visible match of `from` to the first match of `to` below it, as one element. */
  async rows(from: Text, to: Text, options: { timeout?: number } = {}): Promise<Locator> {
    const start = await this.rowIndex(from, options)
    const end = await this.rowIndex(to, options)
    if (end < start) throw new Error(`${to} is above ${from} on screen`)
    return this.mark(start, end)
  }

  /** The text a `row` or `rows` locator covers. */
  async textAt(mark: Locator): Promise<string> {
    return (await mark.getAttribute("data-text")) ?? ""
  }

  private async rowIndex(text: Text, { timeout = 5_000 } = {}) {
    let index = -1
    await expect
      .poll(
        async () => (index = await this.page.evaluate((p) => window.__term!.rowIndexOf(p), pattern(text))),
        { timeout, message: `${text} is not on screen` },
      )
      .toBeGreaterThanOrEqual(0)
    return index
  }

  private async mark(from: number, to: number) {
    const id = await this.page.evaluate(([from, to]) => window.__term!.markRows(from, to), [from, to] as const)
    return this.page.locator(`[data-mark="${id}"]`)
  }
}

export const test = base.extend<{
  terminalSession: TerminalSession
  checkoutEngine: boolean
  shell: boolean | undefined
  terminalEnv: Record<string, string>
  terminalCwd: string | undefined
  terminal: Terminal
}>({
  /** How the `terminal` fixture's session starts; override with `test.use({ terminalSession: ... })`. */
  terminalSession: [{}, { option: true }],
  /** Whether `oe` in the session is this checkout's. The benchmark turns it off and installs its own. */
  checkoutEngine: [true, { option: true }],
  /** Whether the session's command starts a shell to wait for; unset, that is whether there is no command. */
  shell: [undefined, { option: true }],
  /** Environment other fixtures add to the session (the engine daemon's, for one), under the spec's own. */
  terminalEnv: async ({}, use) => {
    await use({})
  },
  /** Where sessions start unless the spec says: another fixture's directory (the engine daemon's repository, for one). */
  terminalCwd: async ({}, use) => {
    await use(undefined)
  },
  terminal: async ({ page, terminalSession, checkoutEngine, shell, terminalEnv, terminalCwd }, use) => {
    await use(
      await Terminal.open(
        page,
        {
          cwd: terminalCwd,
          ...terminalSession,
          env: { ...terminalEnv, ...terminalSession.env },
        },
        { checkoutEngine, ...(shell === undefined ? {} : { shell }) },
      ),
    )
  },
})
