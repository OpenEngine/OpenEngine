import { execFileSync } from "node:child_process"
import { mkdtempSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { test, expect } from "@playwright/test"
import WebSocket from "ws"
import { startTerminalServer, type TerminalServer } from "../terminal/server.ts"
import type { ServerMessage, StartMessage } from "../terminal/protocol.ts"

/** One PTY session over the bridge, as a test sees it. */
async function openSession(server: TerminalServer, start: Omit<StartMessage, "type"> = {}) {
  const socket = new WebSocket(`${server.url.replace(/^http/, "ws")}/pty`)
  await new Promise((resolve, reject) => socket.once("open", resolve).once("error", reject))
  let output = ""
  let resolveExit!: (message: Extract<ServerMessage, { type: "exit" }>) => void
  const exit = new Promise<Extract<ServerMessage, { type: "exit" }>>((resolve) => (resolveExit = resolve))
  const started = new Promise<number>((resolve) => {
    socket.on("message", (raw) => {
      const message = JSON.parse(String(raw)) as ServerMessage
      if (message.type === "started") resolve(message.pid)
      if (message.type === "output") output += message.data
      if (message.type === "exit") resolveExit(message)
    })
  })
  socket.send(JSON.stringify({ type: "start", ...start }))
  const pid = await started
  return {
    pid,
    exit,
    output: () => output,
    type: (data: string) => socket.send(JSON.stringify({ type: "input", data })),
    resize: (cols: number, rows: number) => socket.send(JSON.stringify({ type: "resize", cols, rows })),
    waitFor: (pattern: RegExp) => expect.poll(() => output, { timeout: 5_000 }).toMatch(pattern),
    close: () => socket.close(),
  }
}

/** The pid of `parent`'s child running `name`, or 0; pgrep exits 1 when there is none yet. */
function childNamed(parent: number, name: string) {
  try {
    return Number(execFileSync("pgrep", ["-P", String(parent), name], { encoding: "utf8" }).trim())
  } catch {
    return 0
  }
}

function alive(pid: number) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

test.describe("terminal bridge server", { tag: ["@skip-docs"] }, () => {
  let server: TerminalServer

  test.beforeEach(async () => {
    server = await startTerminalServer({ port: 0 })
  })

  test.afterEach(async () => {
    await server.close()
  })

  test("answers its health check on loopback", async () => {
    expect(new URL(server.url).hostname).toBe("127.0.0.1")
    const response = await fetch(`${server.url}/healthz`)
    expect(response.status).toBe(200)
  })

  test("round-trips input and output through an interactive shell", async () => {
    const session = await openSession(server)
    await session.waitFor(/\$ $/)
    session.type("echo hello-$((40 + 2))\r")
    await session.waitFor(/hello-42/)
    session.close()
  })

  test("uses a fixed prompt and environment that leak nothing from the host", async () => {
    const session = await openSession(server, { env: { GREETING: "hi" } })
    session.type('echo "[$TERM|$GREETING|${FORCE_COLOR-unset}|$PS1]"\r')
    await session.waitFor(/\[xterm-256color\|hi\|unset\|\$ \]/)
    expect(session.output()).not.toContain(os.userInfo().username + "@")
    session.close()
  })

  test("starts at the requested size and follows resizes", async () => {
    const session = await openSession(server, { cols: 80, rows: 24 })
    session.type("stty size\r")
    await session.waitFor(/24 80/)
    session.resize(120, 40)
    session.type("stty size\r")
    await session.waitFor(/40 120/)
    session.close()
  })

  test("runs a requested command in a requested directory and reports its exit code", async () => {
    const cwd = mkdtempSync(path.join(os.tmpdir(), "e2e-cwd-"))
    const session = await openSession(server, {
      command: "/bin/sh",
      args: ["-c", "pwd; exit 3"],
      cwd,
    })
    expect((await session.exit).exitCode).toBe(3)
    expect(session.output()).toContain(path.basename(cwd))
  })

  test("reports the exit code of an interactive shell that exits", async () => {
    const session = await openSession(server)
    session.type("exit 5\r")
    expect((await session.exit).exitCode).toBe(5)
  })

  test("kills the shell and its foreground job when the socket closes", async () => {
    const session = await openSession(server)
    session.type("sleep 300\r")
    let sleeper = 0
    await expect
      .poll(() => {
        sleeper = childNamed(session.pid, "sleep")
        return sleeper
      })
      .toBeGreaterThan(0)
    session.close()
    await expect.poll(() => alive(session.pid) || alive(sleeper), { timeout: 5_000 }).toBe(false)
  })

  test("kills every session when the server closes", async () => {
    const first = await openSession(server)
    const second = await openSession(server)
    await server.close()
    await expect.poll(() => alive(first.pid) || alive(second.pid), { timeout: 5_000 }).toBe(false)
    server = await startTerminalServer({ port: 0 })
  })

  test("serves the page from its static directory and nothing outside it", async () => {
    await server.close()
    const root = mkdtempSync(path.join(os.tmpdir(), "e2e-static-"))
    writeFileSync(path.join(root, "index.html"), "<!doctype html><title>terminal</title>")
    server = await startTerminalServer({ port: 0, staticDir: root })
    const page = await fetch(`${server.url}/`)
    expect(page.headers.get("content-type")).toContain("text/html")
    expect(await page.text()).toContain("<title>terminal</title>")
    expect((await fetch(`${server.url}/..%2f..%2fetc%2fpasswd`)).status).toBe(404)
  })
})
