// A real engine daemon for each test: the production `engine-web`, on a port
// of its own, with its state, configuration and backends file in a throwaway
// directory. Terminal sessions in a spec that uses it reach it as `local`, so
// guides type plain `engine ...` commands.
//
// Per test rather than per worker: graphs, runs and loops are all things one
// test leaves behind, and the doc pass runs every spec in one worker, so a
// shared daemon would make each guide's screenshots depend on what ran first.
//
// Its agents are scripted unless E2E_AGENT=real asks for real ones (see
// resolveAgentMode): the stub is fast and does exactly what a spec needs on
// cue, and real agents are there to show the whole thing works. Specs assert
// on state -- statuses, what was registered, what exists -- and never on what
// an agent said, so one spec passes either way.

import { execFileSync, spawn, type ChildProcess } from "node:child_process"
import { randomBytes } from "node:crypto"
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { engineBin } from "../support/engine.ts"
import { test as terminalTest } from "./terminal.ts"

export { expect } from "./terminal.ts"

/** Long enough for a cold start on a slow machine, short enough to fail rather than hang a CI job. */
const STARTUP_TIMEOUT_MS = 60_000

/** What the daemon is allowed of the host's environment; nothing else, credentials included, reaches it. */
const INHERITED = ["PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG"]

/** What a real agent additionally needs to find its login: never a GitHub credential. */
const AGENT_LOGINS = ["CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CODEX_HOME", "OPENAI_API_KEY"]

/**
 * Where engine-web puts each run's git worktree. It has no setting for this,
 * so a test's runs land here, outside its own directory, and are removed when
 * its daemon stops.
 */
const WORKSPACES = "/tmp/engine-workspaces"

/** Remove the run worktrees whose repository is inside `root`. */
function removeWorkspaces(root: string) {
  if (!existsSync(WORKSPACES)) return
  const roots = [root, realpathSync(root)]
  for (const name of readdirSync(WORKSPACES)) {
    const workspace = path.join(WORKSPACES, name)
    let gitdir = ""
    try {
      gitdir = readFileSync(path.join(workspace, ".git"), "utf8")
    } catch {
      continue
    }
    if (roots.some((prefix) => gitdir.includes(prefix + path.sep))) rmSync(workspace, { recursive: true, force: true })
  }
}

const STUB_DAEMON = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../harness/daemon.py")

export type AgentMode = "real" | "stub"

/** What the scripted agent says and does; see tests/provider_fakes.py. */
export type AgentScript = {
  title?: string
  scenarios: {
    /** Matched as a substring of the prompt; absent, it matches any turn. */
    when?: string
    steps: (
      | { type: "say"; text: string }
      | { type: "run"; command: string }
      | { type: "tool"; name: string; arguments: Record<string, unknown> }
    )[]
  }[]
}

type AgentDetection = { claudeLogin: boolean; npx: boolean }

function detectAgent(): AgentDetection {
  const claudeConfig = process.env.CLAUDE_CONFIG_DIR ?? path.join(os.homedir(), ".claude")
  let npx = true
  try {
    execFileSync("npx", ["--version"], { stdio: "ignore" })
  } catch {
    npx = false
  }
  return { claudeLogin: existsSync(claudeConfig), npx }
}

/**
 * `E2E_AGENT`: `stub` (the default) scripts the agents; `real` runs Claude
 * Code through its ACP adapter, and refuses to start where it can't rather
 * than quietly scripting them instead.
 */
export function resolveAgentMode(
  env: Record<string, string | undefined> = process.env,
  detect: AgentDetection | (() => AgentDetection) = detectAgent,
): AgentMode {
  const requested = env.E2E_AGENT ?? "stub"
  if (requested === "stub") return "stub"
  if (requested !== "real") throw new Error(`E2E_AGENT must be real or stub, not ${JSON.stringify(requested)}`)
  const found = typeof detect === "function" ? detect() : detect
  if (!found.claudeLogin) {
    throw new Error("E2E_AGENT=real needs a Claude Code login: none at $CLAUDE_CONFIG_DIR or ~/.claude")
  }
  if (!found.npx) throw new Error("E2E_AGENT=real needs npx, to fetch Claude Code's ACP adapter")
  return "real"
}

export type Engine = {
  /** Where the daemon answers, http://127.0.0.1:PORT. */
  url: string
  /** The throwaway directory holding its state, configuration and backends file. */
  root: string
  /** The git repository runs check out by default; its `origin` is a bare repository beside it. */
  repository: string
  agentMode: AgentMode
  /** Set what the stub agent will do next; read on every turn. Does nothing for real agents. */
  script(script: AgentScript): void
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

/** A repository with one commit and a bare origin, so a run can base its worktree on origin/main. */
function fixtureRepository(root: string) {
  const repository = path.join(root, "repository")
  const origin = path.join(root, "origin.git")
  const git = (...args: string[]) => execFileSync("git", args, { stdio: "pipe" })
  git("init", "--initial-branch=main", repository)
  writeFileSync(path.join(repository, "README.md"), "# Example project\n")
  git("-C", repository, "add", "--all")
  git("-C", repository, "-c", "user.email=engine@localhost", "-c", "user.name=engine", "commit", "--no-verify", "-m", "Initial commit")
  git("init", "--bare", "--initial-branch=main", origin)
  git("-C", repository, "remote", "add", "origin", origin)
  git("-C", repository, "push", "--quiet", "origin", "main")
  git("-C", repository, "remote", "set-head", "origin", "main")
  return repository
}

export async function startEngine({
  keepRoot = () => false,
  agentMode = resolveAgentMode(),
}: { keepRoot?: () => boolean; agentMode?: AgentMode } = {}): Promise<Engine> {
  const root = mkdtempSync(path.join(os.tmpdir(), "openengine-e2e-"))
  const repository = fixtureRepository(root)
  const scriptPath = path.join(root, "agent-script.json")
  const script = (value: AgentScript) => writeFileSync(scriptPath, JSON.stringify(value, null, 2))
  script({ scenarios: [{ steps: [{ type: "say", text: "Done." }] }] })
  const config = path.join(root, "engine.toml")
  const backendsFile = path.join(root, "backends.json")
  const githubConfig = path.join(root, "gh")
  mkdirSync(githubConfig)
  writeFileSync(config, "# The e2e daemon's configuration: built-in defaults unless a spec says otherwise.\n")
  const token = randomBytes(24).toString("hex")

  const passed = agentMode === "real" ? [...INHERITED, ...AGENT_LOGINS] : INHERITED
  const inherited = Object.fromEntries(passed.flatMap((name) => (process.env[name] ? [[name, process.env[name]!]] : [])))
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
    ...(agentMode === "stub" ? { ENGINE_FAKE_SCRIPT: scriptPath } : {}),
  }
  // Started in the repository: with no [repos] configured, that is what a run checks out.
  const [command, ...args] =
    agentMode === "stub"
      ? [path.join(path.dirname(engineBin), "python"), STUB_DAEMON, path.join(root, "bin")]
      : [engineWebBin()]
  const daemon = spawn(command!, args, { cwd: repository, env: daemonEnv, stdio: ["ignore", "pipe", "pipe"] })
  const output: string[] = []
  daemon.stdout!.on("data", (chunk: Buffer) => output.push(chunk.toString()))
  daemon.stderr!.on("data", (chunk: Buffer) => output.push(chunk.toString()))
  let failure: string | undefined
  daemon.on("error", (error) => (failure = `${command} could not be started: ${error.message}`))

  const stop = async () => {
    await terminate(daemon)
    if (keepRoot()) return
    removeWorkspaces(root)
    rmSync(root, { recursive: true, force: true })
  }

  try {
    const url = await waitUntilServing(daemon, output, () => failure)
    writeFileSync(backendsFile, JSON.stringify({ current: "local", backends: { local: { url } } }, null, 2))
    return {
      url,
      root,
      repository,
      agentMode,
      script,
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
    testInfo.annotations.push({ type: "agents", description: engine.agentMode })
    if (engine.agentMode === "real") console.log(`[engine] ${testInfo.title}: running real agents (E2E_AGENT=stub to script them)`)
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
  // The CLI sends a run "the repository you are in": start sessions in the
  // fixture repository, as a reader of a guide would be in their own project.
  terminalCwd: async ({ engine }, use) => {
    await use(engine.repository)
  },
})
