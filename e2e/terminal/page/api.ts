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
  /** The whole buffer, scrollback included, as plain text without trailing spaces. */
  text(): string
  /** The index of the last visible row matching `pattern` (the nth `.xterm-rows > div`), or -1. */
  rowIndexOf(pattern: Pattern): number
}

declare global {
  interface Window {
    __term?: TerminalTestApi
  }
}
