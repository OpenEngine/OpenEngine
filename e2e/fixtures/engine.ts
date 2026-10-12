// A real engine daemon for each test: the production `engine-web`, on a port
// of its own, with its state, configuration and backends file in a throwaway
// directory. Terminal sessions in a spec that uses it reach it as `local`, so
// guides type plain `engine ...` commands.
//
// Per test rather than per worker: graphs, runs and loops are all things one
// test leaves behind, and the doc pass runs every spec in one worker, so a
// shared daemon would make each guide's screenshots depend on what ran first.

import { spawn, type ChildProcess } from "node:child_process"
import { randomBytes } from "node:crypto"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { engineBin } from "../support/engine.ts"
import { test as terminalTest } from "./terminal.ts"

export { expect } from "./terminal.ts"

/** Long enough for a cold start on a slow machine, short enough to fail rather than hang a CI job. */
const STARTUP_TIMEOUT_MS = 60_000

/** What the daemon is allowed of the host's environment; nothing else, credentials included, reaches it. */
const INHERITED = ["PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG"]

export type Engine = {
  /** Where the daemon answers, http://127.0.0.1:PORT. */
  url: string
  /** The throwaway directory holding its state, configuration and backends file. */
  root: string
  /** What a terminal session needs to reach it as `local`. */
  env: Record<string, string>
  /** The whole environment the daemon runs with. */
  daemonEnv: Record<string, string>
  /** Everything the daemon has printed. */
  log(): string
  stop(): Promise<void>
}

/** `engine-web` beside the `engine` the guides drive, so both come from the same install. */
function engineWebBin() {
  return process.env.ENGINE_WEB_BIN ?? path.join(path.dirname(engineBin), "engine-web")
}

export async function startEngine({ keepRoot = () => false }: { keepRoot?: () => boolean } = {}): Promise<Engine> {
  const root = mkdtempSync(path.join(os.tmpdir(), "openengine-e2e-"))
  const config = path.join(root, "engine.toml")
  const backendsFile = path.join(root, "backends.json")
  const githubConfig = path.join(root, "gh")
  mkdirSync(githubConfig)
  writeFileSync(config, "# The e2e daemon's configuration: built-in defaults unless a spec says otherwise.\n")
  const token = randomBytes(24).toString("hex")

  const inherited = Object.fromEntries(INHERITED.flatMap((name) => (process.env[name] ? [[name, process.env[name]!]] : [])))
  const daemonEnv: Record<string, string> = {
    ...inherited,
    ENGINE_CONFIG: config,
    ENGINE_HOST: "127.0.0.1",
    // The OS picks the port and the daemon reports it, so parallel tests never race for one.
    ENGINE_PORT: "0",
    ENGINE_STATE_DIRECTORY: path.join(root, "state"),
    ENGINE_SERVICE_TOKEN: token,
    // An empty gh configuration and no token: the daemon must never act on GitHub as whoever runs the tests.
    GH_CONFIG_DIR: githubConfig,
  }
  const daemon = spawn(engineWebBin(), [], { cwd: root, env: daemonEnv, stdio: ["ignore", "pipe", "pipe"] })
  const output: string[] = []
  daemon.stdout!.on("data", (chunk: Buffer) => output.push(chunk.toString()))
  daemon.stderr!.on("data", (chunk: Buffer) => output.push(chunk.toString()))
  let failure: string | undefined
  daemon.on("error", (error) => (failure = `${engineWebBin()} could not be started: ${error.message}`))

  const stop = async () => {
    await terminate(daemon)
    if (!keepRoot()) rmSync(root, { recursive: true, force: true })
  }

  try {
    const url = await waitUntilServing(daemon, output, () => failure)
    writeFileSync(backendsFile, JSON.stringify({ current: "local", backends: { local: { url } } }, null, 2))
    return {
      url,
      root,
      env: { ENGINE_BACKENDS_FILE: backendsFile, ENGINE_SERVICE_TOKEN: token },
      daemonEnv,
      log: () => output.join(""),
      stop,
    }
  } catch (error) {
    await stop()
    throw error
  }
}

async function waitUntilServing(daemon: ChildProcess, output: string[], failure: () => string | undefined) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS
  while (Date.now() < deadline) {
    if (failure()) throw new Error(failure())
    if (daemon.exitCode !== null) throw new Error(`the engine daemon exited ${daemon.exitCode}:\n${output.join("")}`)
    const url = output.join("").match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/)?.[1]
    if (url) {
      try {
        if ((await fetch(`${url}/api/config`)).ok) return url
      } catch {
        // Bound but not answering yet.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`the engine daemon did not start in time:\n${output.join("")}`)
}

async function terminate(daemon: ChildProcess) {
  if (daemon.exitCode !== null || daemon.signalCode !== null) return
  const exited = new Promise((resolve) => daemon.once("exit", resolve))
  daemon.kill("SIGTERM")
  const killed = setTimeout(() => daemon.kill("SIGKILL"), 10_000)
  await exited
  clearTimeout(killed)
}

export const test = terminalTest.extend<{ engine: Engine }>({
  engine: async ({}, use, testInfo) => {
    const failed = () => testInfo.status !== testInfo.expectedStatus
    const engine = await startEngine({ keepRoot: failed })
    try {
      await use(engine)
    } finally {
      await engine.stop()
      if (failed()) {
        // What the daemon said usually explains the failure; its directory holds the databases.
        await testInfo.attach("engine daemon", { body: engine.log(), contentType: "text/plain" })
        console.log(`[engine] kept the failed test's daemon directory: ${engine.root}`)
      }
    }
  },
  terminalEnv: async ({ engine }, use) => {
    await use(engine.env)
  },
})
