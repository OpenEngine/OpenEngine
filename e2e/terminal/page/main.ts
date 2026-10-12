// A browser terminal on one PTY session from the bridge server. The session
// comes from the query string: token, cmd, repeated arg, cwd, env (JSON),
// cols and rows. Everything about how it looks is fixed, so the same output
// draws the same pixels every time.

import "@fontsource/jetbrains-mono/400.css"
import "@fontsource/jetbrains-mono/700.css"
import "@xterm/xterm/css/xterm.css"
import "./page.css"
import { Terminal } from "@xterm/xterm"
import type { ClientMessage, ServerMessage } from "../protocol.ts"
import type { Pattern, TerminalTestApi } from "./api.ts"

const FONT = '"JetBrains Mono"'
const params = new URLSearchParams(location.search)
const cols = Number(params.get("cols") ?? 100)
const rows = Number(params.get("rows") ?? 30)

// xterm measures its cells when it opens, so the font has to be there first.
await Promise.all([document.fonts.load(`14px ${FONT}`), document.fonts.load(`bold 14px ${FONT}`)])

// The default renderer is the DOM one: every row is an element Playwright can
// find and test2doc can annotate. Don't load the WebGL or canvas addons.
const terminal = new Terminal({
  cols,
  rows,
  fontFamily: `${FONT}, monospace`,
  fontSize: 14,
  lineHeight: 1.2,
  cursorBlink: false,
  scrollback: 5000,
  theme: {
    background: "#0d1117",
    foreground: "#e6edf3",
    cursor: "#e6edf3",
    selectionBackground: "#264f78",
    black: "#484f58",
    red: "#ff7b72",
    green: "#3fb950",
    yellow: "#d29922",
    blue: "#58a6ff",
    magenta: "#bc8cff",
    cyan: "#39c5cf",
    white: "#b1bac4",
    brightBlack: "#6e7681",
    brightRed: "#ffa198",
    brightGreen: "#56d364",
    brightYellow: "#e3b341",
    brightBlue: "#79c0ff",
    brightMagenta: "#d2a8ff",
    brightCyan: "#56d4dd",
    brightWhite: "#ffffff",
  },
})
terminal.open(document.getElementById("terminal")!)

function matcher(pattern: Pattern): (line: string) => boolean {
  if (typeof pattern === "string") return (line) => line.includes(pattern)
  const regexp = new RegExp(pattern.source, pattern.flags)
  return (line) => regexp.test(line)
}

const api: TerminalTestApi = {
  ready: false,
  exited: false,
  outputCount: 0,
  lastOutputAt: 0,
  text() {
    const buffer = terminal.buffer.active
    const lines: string[] = []
    for (let i = 0; i < buffer.length; i++) lines.push(buffer.getLine(i)?.translateToString(true) ?? "")
    return lines.join("\n").trimEnd()
  },
  rowIndexOf(pattern) {
    const matches = matcher(pattern)
    const buffer = terminal.buffer.active
    for (let row = terminal.rows - 1; row >= 0; row--) {
      if (matches(buffer.getLine(buffer.viewportY + row)?.translateToString(true) ?? "")) return row
    }
    return -1
  },
}
window.__term = api

const socket = new WebSocket(`ws://${location.host}/pty?token=${encodeURIComponent(params.get("token") ?? "")}`)
const send = (message: ClientMessage) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))

socket.addEventListener("open", () => {
  const args = params.getAll("arg")
  send({
    type: "start",
    ...(params.get("cmd") ? { command: params.get("cmd")!, args } : {}),
    ...(params.get("cwd") ? { cwd: params.get("cwd")! } : {}),
    ...(params.get("env") ? { env: JSON.parse(params.get("env")!) as Record<string, string> } : {}),
    cols,
    rows,
  })
})

socket.addEventListener("message", (event) => {
  const message = JSON.parse(String(event.data)) as ServerMessage
  if (message.type === "started") {
    api.pid = message.pid
    api.ready = true
  } else if (message.type === "output") {
    // Counted once xterm has parsed it, so text() already includes it.
    terminal.write(message.data, () => {
      api.outputCount++
      api.lastOutputAt = performance.now()
    })
  } else if (message.type === "exit") {
    // After the output already queued, so a test that sees `exited` sees all of it.
    terminal.write("", () => {
      api.exitCode = message.exitCode
      api.exited = true
    })
  } else if (message.type === "error") {
    api.error = message.message
    terminal.write(`\r\n\x1b[31m${message.message}\x1b[0m\r\n`)
  }
})

// A refused upgrade only ever surfaces as a close before the session started.
socket.addEventListener("close", () => {
  if (api.ready || api.error !== undefined) return
  api.error = "could not connect to the terminal server"
  terminal.write(`\x1b[31m${api.error}\x1b[0m`)
})

terminal.onData((data) => send({ type: "input", data }))
terminal.focus()
