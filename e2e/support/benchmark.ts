import { execFile } from "node:child_process"
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { promisify } from "node:util"

const run = promisify(execFile)

/** The benchmark's calculator repository: always this name, deleted before each run makes a new one. */
export const BENCH_REPO = "oe-bench"
/** A repository is only ever deleted if its name starts with `BENCH_REPO` and it carries this topic. */
export const BENCH_TOPIC = "oe-bench"

/** The local folder holding OpenEngine and the run's state; `OE_BENCH_DIR` moves it. */
export const benchDirectory = () => process.env.OE_BENCH_DIR ?? path.join(os.tmpdir(), "oe-bench")

export class GhError extends Error {
  status?: number
  constructor(message: string, status?: number) {
    super(message)
    this.status = status
  }
}

/** Teardown was asked to delete something that isn't the benchmark's. Nothing was changed. */
export class TeardownRefused extends Error {}

/** Run `gh` as the account it is logged in as and return stdout. */
export async function gh(...args: string[]): Promise<string> {
  try {
    return (await run("gh", args, { maxBuffer: 16 * 1024 * 1024 })).stdout
  } catch (error) {
    const { stderr = "", message } = error as { stderr?: string; message: string }
    const status = /HTTP (\d{3})/.exec(stderr)?.[1]
    throw new GhError(`gh ${args.slice(0, 3).join(" ")} failed: ${stderr.trim() || message}`, status ? Number(status) : undefined)
  }
}

/** The account `gh` is logged in as. */
export async function whoami(): Promise<string> {
  return (await gh("api", "user", "--jq", ".login")).trim()
}

/** The OAuth scopes of the token, or undefined if GitHub doesn't report any (a fine-grained token). */
export async function tokenScopes(): Promise<string[] | undefined> {
  const line = (await gh("api", "-i", "user")).split(/\r?\n/).find((l) => /^x-oauth-scopes:/i.test(l))
  return line === undefined ? undefined : line.slice(line.indexOf(":") + 1).split(",").map((s) => s.trim()).filter(Boolean)
}

export type RepoInfo = { name: string; topics: string[] }

/** The repository, or undefined if there is no such repository. */
export async function getRepo(owner: string, name: string): Promise<RepoInfo | undefined> {
  try {
    const repo = JSON.parse(await gh("api", `repos/${owner}/${name}`)) as { name: string; topics?: string[] }
    return { name: repo.name, topics: repo.topics ?? [] }
  } catch (error) {
    if (error instanceof GhError && error.status === 404) return undefined
    throw error
  }
}

type Tracked = { pid: number; command: string }

const stateFile = (directory: string) => path.join(directory, "processes.json")

/** The command line of a running process, or undefined if there is none. */
async function commandOf(pid: number): Promise<string | undefined> {
  try {
    return (await run("ps", ["-o", "command=", "-p", String(pid)])).stdout.trim() || undefined
  } catch {
    return undefined
  }
}

/**
 * Remember a process this run started (a tunnel, a daemon) so teardown can stop it.
 * Teardown stops only these, found by the state file, never by process name, so a
 * tunnel of the user's own is left alone.
 */
export async function recordProcess(pid: number, directory = benchDirectory()) {
  const command = await commandOf(pid)
  if (command === undefined) throw new Error(`Process ${pid} is not running, so it can't be recorded.`)
  mkdirSync(directory, { recursive: true })
  const tracked: Tracked[] = existsSync(stateFile(directory)) ? JSON.parse(readFileSync(stateFile(directory), "utf8")) : []
  writeFileSync(stateFile(directory), JSON.stringify([...tracked, { pid, command }]))
}

/**
 * Stop every recorded process that is still the process that was recorded, and leave
 * everything else (the repository, the folder) for a person to look at. Returns how many
 * were stopped. A run does this when it ends, however it ends.
 */
export async function stopProcesses(directory = benchDirectory()): Promise<number> {
  if (!existsSync(stateFile(directory))) return 0
  let stopped = 0
  for (const { pid, command } of JSON.parse(readFileSync(stateFile(directory), "utf8")) as Tracked[]) {
    // The pid may have been reused by something else since: only stop the same command.
    if ((await commandOf(pid)) !== command) continue
    process.kill(pid, "SIGTERM")
    for (let waited = 0; (await commandOf(pid)) === command && waited < 5_000; waited += 100) {
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    if ((await commandOf(pid)) === command) process.kill(pid, "SIGKILL")
    stopped++
  }
  return stopped
}

/** A folder safe to delete wholesale: named for the benchmark, and not a place the user lives. */
function assertRemovable(directory: string) {
  const resolved = path.resolve(directory)
  if (!path.basename(resolved).startsWith(BENCH_REPO) || resolved === os.homedir() || resolved === path.parse(resolved).root) {
    throw new TeardownRefused(`Refusing to delete ${resolved}: the benchmark folder must be named ${BENCH_REPO}…`)
  }
}

export type TeardownResult = { owner: string; deletedRepo: boolean; removedDirectory: boolean; stoppedProcesses: number }

/**
 * Remove everything a benchmark run leaves behind: the calculator repository
 * (and its Pages site), the local folder, and any process the run recorded.
 * Safe to run twice, and on a machine with nothing to remove.
 *
 * It refuses, changing nothing, a repository whose name doesn't start with
 * `oe-bench` or that lacks the `oe-bench` topic.
 */
export async function teardown({ repo = BENCH_REPO, directory = benchDirectory() } = {}): Promise<TeardownResult> {
  const owner = await whoami()
  const found = await getRepo(owner, repo)
  if (found && !found.name.startsWith(BENCH_REPO)) {
    throw new TeardownRefused(`Refusing to delete ${owner}/${found.name}: its name doesn't start with ${BENCH_REPO}.`)
  }
  if (found && !found.topics.includes(BENCH_TOPIC)) {
    throw new TeardownRefused(`Refusing to delete ${owner}/${found.name}: it doesn't have the ${BENCH_TOPIC} topic.`)
  }
  assertRemovable(directory)
  if (found && !((await tokenScopes()) ?? ["delete_repo"]).includes("delete_repo")) {
    throw new Error("The gh token needs the delete_repo scope to delete the benchmark repository: run `gh auth refresh -s delete_repo`.")
  }

  const stoppedProcesses = await stopProcesses(directory)
  if (found) {
    try {
      await gh("api", "-X", "DELETE", `repos/${owner}/${found.name}/pages`)
    } catch (error) {
      if (!(error instanceof GhError && error.status === 404)) throw error
    }
    await gh("api", "-X", "DELETE", `repos/${owner}/${found.name}`)
    // Deletion is asynchronous: wait until the name is free so a new repo can take it.
    for (let waited = 0; (await getRepo(owner, found.name)) && waited < 30_000; waited += 500) {
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
  }
  const removedDirectory = existsSync(directory)
  rmSync(directory, { recursive: true, force: true })
  return { owner, deletedRepo: found !== undefined, removedDirectory, stoppedProcesses }
}
