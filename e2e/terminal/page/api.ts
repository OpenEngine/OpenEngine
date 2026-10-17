// What the terminal page exposes to Playwright as `window.__term`.

/** A text to find, or a RegExp's source and flags (a RegExp can't cross `page.evaluate`). */
export type Pattern = string | { source: string; flags?: string }

export type TerminalTestApi = {
  /** The PTY has started and output is flowing. */
  ready: boolean
  pid?: number
  exited: boolean
  exitCode?: number
  /** Why the session could not start or was refused. */
  error?: string
  /** How many output chunks have been drawn; unchanged for a while means the screen has settled. */
  outputCount: number
  /** `performance.now()` when the last output chunk was drawn, or 0. */
  lastOutputAt: number
  /** How many prompts the shell has drawn: one when it is ready, one more after each command. */
  promptCount: number
  /** The exit code of the command before the latest prompt. */
  lastExitCode?: number
  /**
   * The whole buffer, scrollback included, as plain text: lines the terminal
   * wrapped are joined back together and trailing spaces are dropped.
   */
  text(): string
  /** Show the beginning of the scrollback for long command output. */
  scrollToTop(): void
  /** The index of the last visible row matching `pattern` (the nth `.xterm-rows > div`), or -1. */
  rowIndexOf(pattern: Pattern): number
  /**
   * Cover visible rows `from` to `to` with one transparent element hugging
   * their text, for a screenshot to highlight. Returns its `data-mark`; the
   * element's `data-text` holds the text it covers.
   */
  markRows(from: number, to: number): string
  /** The page area from the top of the frame to the last visible row with text or the cursor, plus the frame's bottom padding. */
  usedArea(): { x: number; y: number; width: number; height: number }
}

declare global {
  interface Window {
    __term?: TerminalTestApi
  }
}
