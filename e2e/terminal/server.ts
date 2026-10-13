// The bridge between a browser terminal and a real one: each WebSocket on
// /pty is one PTY, started by the client's `start` message and killed when the
// socket closes. Test infrastructure only: it runs whatever a client asks for,
// so it listens on loopback, answers only requests addressed to loopback (no
// DNS rebinding), and opens a session only for a client holding this run's
// token whose page, if it has one, is this server's own.

import { randomBytes, timingSafeEqual } from "node:crypto"
import { readFile } from "node:fs/promises"
import { createServer, type IncomingMessage, type ServerResponse } from "node:http"
import type { AddressInfo } from "node:net"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { parseArgs } from "node:util"
import pty from "node-pty"
import { WebSocketServer, type WebSocket } from "ws"
import type { ClientMessage, ServerMessage, StartMessage } from "./protocol.ts"

const HOST = "127.0.0.1"
const DEFAULT_SIZE = { cols: 100, rows: 30 }
/** How long a shell gets to exit on SIGHUP before it and its jobs are killed. */
const HANGUP_GRACE_MS = 2_000

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".woff2": "font/woff2",
  ".map": "application/json",
}

export type TerminalServer = {
  url: string
  port: number
  /** What a client passes as `/pty?token=` to open a session. */
  token: string
  close(): Promise<void>
}

export type TerminalServerOptions = {
  /** 0 picks a free port. */
  port?: number
  /** Where the terminal page is served from; without it only /healthz answers. */
  staticDir?: string
  /** Defaults to a fresh random token. */
  token?: string
}

/**
 * What every session starts from, so screenshots never show the host's prompt,
 * rc files, history or colour settings. Only what a CLI needs to run is taken
 * from the host. The PTY is a real terminal, so tools decide on colour
 * themselves; Playwright's FORCE_COLOR is not passed on.
 */
function baseEnvironment(): Record<string, string> {
  const inherited = Object.fromEntries(
    ["PATH", "HOME", "USER", "LOGNAME", "TMPDIR"].flatMap((name) =>
      process.env[name] === undefined ? [] : [[name, process.env[name]!]],
    ),
  )
  return {
    ...inherited,
    TERM: "xterm-256color",
    COLORTERM: "truecolor",
    LANG: process.platform === "darwin" ? "en_US.UTF-8" : "C.UTF-8",
    PS1: "$ ",
    // Shell integration (OSC 133, as VS Code's terminal uses it): before each
    // prompt bash reports the last command's exit code in an escape sequence
    // the page counts and nothing draws, so a test knows when a command is done.
    PROMPT_COMMAND: 'printf "\\033]133;D;%s\\007" "$?"',
    HISTFILE: "/dev/null",
    BASH_SILENCE_DEPRECATION_WARNING: "1",
  }
}

function send(socket: WebSocket, message: ServerMessage) {
  if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(message))
}

function alive(pid: number) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

class Session {
  readonly terminal: pty.IPty
  exited = false

  constructor(socket: WebSocket, start: StartMessage) {
    const { cols, rows } = { ...DEFAULT_SIZE, ...start }
    this.terminal = pty.spawn(
      start.command ?? "/bin/bash",
      start.command ? (start.args ?? []) : ["--noprofile", "--norc", "-i"],
      {
        name: "xterm-256color",
        cols,
        rows,
        cwd: start.cwd ?? process.cwd(),
        env: { ...baseEnvironment(), ...start.env },
      },
    )
    this.terminal.onData((data) => send(socket, { type: "output", data }))
    this.terminal.onExit(({ exitCode, signal }) => {
      this.exited = true
      send(socket, { type: "exit", exitCode, ...(signal ? { signal } : {}) })
      socket.close()
    })
    send(socket, { type: "started", pid: this.terminal.pid })
  }

  handle(message: ClientMessage) {
    if (message.type === "input") this.terminal.write(message.data)
    if (message.type === "resize") this.terminal.resize(message.cols, message.rows)
  }

  /**
   * Hang up like a closed terminal window: an interactive shell passes SIGHUP
   * on to its jobs. Whatever is still running after the grace period is killed
   * with its process group.
   */
  async terminate() {
    if (this.exited) return
    const pid = this.terminal.pid
    this.terminal.kill("SIGHUP")
    const deadline = Date.now() + HANGUP_GRACE_MS
    while (alive(pid) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50))
    if (alive(pid)) {
      try {
        process.kill(-pid, "SIGKILL")
      } catch {
        this.terminal.kill("SIGKILL")
      }
    }
  }
}

async function serveStatic(root: string | undefined, request: IncomingMessage, response: ServerResponse) {
  const pathname = decodeURIComponent(new URL(request.url ?? "/", "http://localhost").pathname)
  const file = root && path.resolve(root, "." + (pathname.endsWith("/") ? pathname + "index.html" : pathname))
  if (!root || !file || !file.startsWith(path.resolve(root) + path.sep)) {
    response.writeHead(404).end()
    return
  }
  try {
    const body = await readFile(file)
    response.writeHead(200, { "content-type": CONTENT_TYPES[path.extname(file)] ?? "application/octet-stream" })
    response.end(body)
  } catch {
    response.writeHead(404).end()
  }
}

function sameSecret(given: string | null, expected: string) {
  const a = Buffer.from(given ?? "")
  const b = Buffer.from(expected)
  return a.length === b.length && timingSafeEqual(a, b)
}

export async function startTerminalServer(options: TerminalServerOptions = {}): Promise<TerminalServer> {
  const token = options.token ?? randomBytes(24).toString("base64url")
  const sessions = new Set<Session>()
  let port = 0
  const addressedToUs = (request: IncomingMessage) =>
    request.headers.host === `${HOST}:${port}` || request.headers.host === `localhost:${port}`
  // Browsers always send Origin on a WebSocket upgrade; only a non-browser
  // client (which no web page can drive) leaves it out.
  const fromOurPage = (request: IncomingMessage) =>
    request.headers.origin === undefined ||
    request.headers.origin === `http://${HOST}:${port}` ||
    request.headers.origin === `http://localhost:${port}`

  const http = createServer((request, response) => {
    if (!addressedToUs(request)) {
      response.writeHead(403).end()
      return
    }
    if (request.url === "/healthz") {
      response.writeHead(200, { "content-type": "text/plain" }).end("ok")
      return
    }
    void serveStatic(options.staticDir, request, response)
  })
  const sockets = new WebSocketServer({ noServer: true })
  http.on("upgrade", (request, socket, head) => {
    const url = new URL(request.url ?? "/", "http://localhost")
    const allowed =
      url.pathname === "/pty" &&
      addressedToUs(request) &&
      fromOurPage(request) &&
      sameSecret(url.searchParams.get("token"), token)
    if (!allowed) {
      socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
      return
    }
    sockets.handleUpgrade(request, socket, head, (client) => sockets.emit("connection", client, request))
  })

  sockets.on("connection", (socket) => {
    let session: Session | undefined
    socket.on("message", (raw) => {
      let message: ClientMessage
      try {
        message = JSON.parse(String(raw)) as ClientMessage
      } catch {
        send(socket, { type: "error", message: "messages must be JSON" })
        return
      }
      if (session) {
        session.handle(message)
        return
      }
      if (message.type !== "start") {
        send(socket, { type: "error", message: "the first message must be start" })
        return
      }
      try {
        session = new Session(socket, message)
        sessions.add(session)
      } catch (error) {
        send(socket, { type: "error", message: String(error) })
        socket.close()
      }
    })
    socket.on("close", () => {
      if (!session) return
      const closing = session
      void closing.terminate().finally(() => sessions.delete(closing))
    })
  })

  await new Promise<void>((resolve) => http.listen(options.port ?? 0, HOST, resolve))
  port = (http.address() as AddressInfo).port
  return {
    url: `http://${HOST}:${port}`,
    port,
    token,
    async close() {
      await Promise.all([...sessions].map((session) => session.terminate()))
      sessions.clear()
      for (const client of sockets.clients) client.terminate()
      await new Promise<void>((resolve) => sockets.close(() => resolve()))
      await new Promise<void>((resolve) => http.close(() => resolve()))
    },
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { values } = parseArgs({ options: { port: { type: "string" }, static: { type: "string" } } })
  const server = await startTerminalServer({
    port: Number(values.port ?? 0),
    staticDir: values.static,
    // From the environment rather than argv, so it doesn't show up in `ps`.
    token: process.env.E2E_TERMINAL_TOKEN,
  })
  console.log(`terminal server listening on ${server.url}`)
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => void server.close().then(() => process.exit(0)))
  }
}
