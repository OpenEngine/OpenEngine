import { execFile } from "node:child_process"
import { createHash } from "node:crypto"
import { existsSync, readFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"
import type { TerminalSession } from "../fixtures/terminal.ts"

const run = promisify(execFile)

/** The benchmark's calculator repository: always this name, deleted before each run makes a new one. */
export const BENCH_REPO = "oe-bench"
/** A repository is only ever deleted if its name starts with `BENCH_REPO` and it carries this topic. */
export const BENCH_TOPIC = "oe-bench"

/** The Docker image and container the journey runs in: a clean computer, with nothing of this one's setup in it. */
export const BENCH_IMAGE = "oe-bench"
export const BENCH_CONTAINER = "oe-bench"

/** The container user's home, where a person's own configuration and logins live. */
const CONTAINER_HOME = "/home/node"

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

const docker = (...args: string[]) => run("docker", args, { maxBuffer: 16 * 1024 * 1024 })

/** Stop and delete the benchmark's computer, and with it anything running on it. True if there was one. */
export async function removeContainer(): Promise<boolean> {
  const { stdout } = await docker("ps", "-aq", "--filter", `name=^${BENCH_CONTAINER}$`)
  if (!stdout.trim()) return false
  await docker("rm", "-f", BENCH_CONTAINER)
  return true
}

/**
 * Start a new clean computer for the journey. Docker caches the image, so only the first run builds it.
 *
 * The logins it needs come from this computer, each given where a person's own would be: GitHub's, Claude's and
 * Codex's. They are mounted read-only and copied in, so a token the container refreshes never changes this
 * computer's, and removing the container can never remove them.
 */
export async function startContainer() {
  // Built only when the Dockerfile has changed: a build asks the registry about its base image even when nothing
  // changed, which needs the credentials helper (and, on a Mac over SSH, an unlocked keychain), for no reason.
  const context = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "benchmark")
  const version = createHash("sha256").update(readFileSync(path.join(context, "Dockerfile"))).digest("hex").slice(0, 16)
  const built = await docker("image", "inspect", "-f", '{{ index .Config.Labels "oe.bench.dockerfile" }}', BENCH_IMAGE).then(
    ({ stdout }) => stdout.trim(),
    () => "",
  )
  if (built !== version) await docker("build", "--label", `oe.bench.dockerfile=${version}`, "-t", BENCH_IMAGE, context)
  const real = os.homedir()
  const logins = [
    ["gh", process.env.GH_CONFIG_DIR ?? path.join(real, ".config", "gh")],
    ["claude", process.env.CLAUDE_CONFIG_DIR ?? path.join(real, ".claude")],
    ["codex", process.env.CODEX_HOME ?? path.join(real, ".codex")],
  ].filter(([, directory]) => existsSync(directory!))
  await docker("run", "-d", "--init", "--name", BENCH_CONTAINER, ...logins.flatMap(([name, directory]) => ["-v", `${directory}:/logins/${name}:ro`]), BENCH_IMAGE)
  const copy = [
    "mkdir -p ~/.config ~/.claude ~/.codex",
    "[ -d /logins/gh ] && cp -a /logins/gh ~/.config/gh",
    // Only what signs the agents in, not the history and caches around it. Claude keeps its settings file beside its
    // login when it has its own folder, and in the home folder when it doesn't.
    "[ -f /logins/claude/.credentials.json ] && cp /logins/claude/.credentials.json ~/.claude/",
    "{ [ -f /logins/claude/.claude.json ] && cp /logins/claude/.claude.json ~/.claude.json; } || true",
    "[ -f /logins/codex/auth.json ] && cp /logins/codex/auth.json ~/.codex/",
    "true",
  ].join("; ")
  await docker("exec", BENCH_CONTAINER, "sh", "-c", copy)
}

/**
 * A terminal session inside the benchmark's computer, shown to the guide as a shell on a clean machine. The
 * terminal's own base environment (a plain prompt, and the marker that tells a test a command is done) is
 * given to the shell inside, since it can't reach through `docker exec`.
 */
export function containerSession({ cols = 140, rows = 40, env = {} }: { cols?: number; rows?: number; env?: Record<string, string> } = {}): TerminalSession {
  const inside = {
    TERM: "xterm-256color",
    COLORTERM: "truecolor",
    LANG: "C.UTF-8",
    PS1: "$ ",
    PROMPT_COMMAND: 'printf "\\033]133;D;%s\\007" "$?"',
    HISTFILE: "/dev/null",
    GH_NO_UPDATE_NOTIFIER: "1",
    // The tunnel's startup checks print a screenful a second after its address, which scrolls the address out of view.
    TUNNEL_NO_PRECHECKS: "true",
    // Vim as it comes, so the screenshot is the same everywhere.
    VIMINIT: "set nocompatible number ttimeout ttimeoutlen=50",
    ...env,
  }
  return {
    command: "docker",
    args: [
      "exec", "-it", "-w", CONTAINER_HOME,
      ...Object.entries(inside).flatMap(([name, value]) => ["-e", `${name}=${value}`]),
      BENCH_CONTAINER, "bash", "--noprofile", "--norc", "-i",
    ],
    cols,
    rows,
  }
}

export type TeardownResult = { owner: string; deletedRepo: boolean; removedContainer: boolean }

/**
 * Remove everything a benchmark run leaves behind: the calculator repository (and its Pages site) and the
 * container, with the daemon and tunnel running in it. Safe to run twice, and on a machine with nothing to remove.
 *
 * It refuses, changing nothing, a repository whose name doesn't start with
 * `oe-bench` or that lacks the `oe-bench` topic.
 */
export async function teardown({ repo = BENCH_REPO } = {}): Promise<TeardownResult> {
  const owner = await whoami()
  const found = await getRepo(owner, repo)
  if (found && !found.name.startsWith(BENCH_REPO)) {
    throw new TeardownRefused(`Refusing to delete ${owner}/${found.name}: its name doesn't start with ${BENCH_REPO}.`)
  }
  if (found && !found.topics.includes(BENCH_TOPIC)) {
    throw new TeardownRefused(`Refusing to delete ${owner}/${found.name}: it doesn't have the ${BENCH_TOPIC} topic.`)
  }
  if (found && !((await tokenScopes()) ?? ["delete_repo"]).includes("delete_repo")) {
    throw new Error("The gh token needs the delete_repo scope to delete the benchmark repository: run `gh auth refresh -s delete_repo`.")
  }

  const removedContainer = await removeContainer()
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
  return { owner, deletedRepo: found !== undefined, removedContainer }
}
