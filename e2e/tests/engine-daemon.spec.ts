import { spawnSync } from "node:child_process"
import { existsSync, readdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { test, expect, startEngine } from "../fixtures/engine.ts"

const EXAMPLE_GRAPH = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../docs/examples/graphs/dead-code.yaml")

test.describe("oe daemon fixture", { tag: ["@skip-docs"] }, () => {
  test("serves the CLI as `local`, so specs type plain engine commands", async ({ engine, terminal }) => {
    const { output } = await terminal.run("oe backends list --check")
    expect(output).toMatch(new RegExp(`\\*\\s+local\\s+${engine.url.replace(/[.]/g, "\\.")}\\s.*\\bup\\b`))
  })

  test("keeps its state in a directory of its own", async ({ engine }) => {
    expect(existsSync(path.join(engine.root, "state", "conversations.sqlite3"))).toBe(true)
    expect(engine.root.startsWith(path.resolve(process.env.HOME ?? "~", ".local"))).toBe(false)
  })

  test("takes no GitHub credential from the host", async ({ engine }) => {
    // The daemon acts on GitHub as `gh` does in its environment: as nobody.
    expect(Object.keys(engine.daemonEnv).filter((name) => /TOKEN$/.test(name) && name !== "ENGINE_SERVICE_TOKEN")).toEqual([])
    const gh = spawnSync("gh", ["auth", "status"], { env: engine.daemonEnv, encoding: "utf8" })
    expect(gh.status).not.toBe(0)
    expect(gh.stdout + gh.stderr).toMatch(/not logged in/i)
  })

  test("registers and lists graphs through the CLI", async ({ terminal }) => {
    await terminal.run(`oe graph add ${EXAMPLE_GRAPH}`)
    expect((await terminal.run("oe graphs list")).output).toMatch(/^dead-code\s+1\s/m)
  })

  test.describe("with another computer", () => {
    test.use({ anotherComputerAt: "mac-mini.local:4364" })

    test("reaches a second daemon at the address a spec names", async ({ engine, anotherComputer, terminal }) => {
      expect(anotherComputer!.url).not.toBe(engine.url)
      expect(anotherComputer!.root).not.toBe(engine.root)
      await terminal.run("oe backend add mac-mini http://mac-mini.local:4364")
      const { output } = await terminal.run("oe backends list --check")
      expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
      expect(output).toMatch(/^\s+mac-mini\s+http:\/\/mac-mini\.local:4364\s.*\bup\b/m)
    })

    test("reaches no other address through it", async ({ terminal }) => {
      await terminal.run("oe backend add elsewhere http://elsewhere.local:4364")
      expect((await terminal.run("oe backends list --check")).output).toMatch(/^\s+elsewhere\s.*\bdown\b/m)
    })
  })

  test("has no other computer unless a spec asks for one", async ({ anotherComputer }) => {
    expect(anotherComputer).toBeUndefined()
  })

  test("starts a separate daemon each time", async ({ engine }) => {
    const other = await startEngine()
    try {
      expect(other.url).not.toBe(engine.url)
      expect(other.root).not.toBe(engine.root)
    } finally {
      await other.stop()
    }
  })

  test("leaves none of its runs' worktrees behind", async ({ page }) => {
    const { Terminal } = await import("../fixtures/terminal.ts")
    const engine = await startEngine({ agentMode: "stub" })
    const root = realpathSync(engine.root)
    const workspaces = "/tmp/engine-workspaces"
    const ours = () =>
      (existsSync(workspaces) ? readdirSync(workspaces) : []).filter((name) => {
        try {
          return readFileSync(path.join(workspaces, name, ".git"), "utf8").includes(root)
        } catch {
          return false
        }
      })
    try {
      const terminal = await Terminal.open(page, { cwd: engine.repository, env: engine.env })
      const graph = path.join(engine.root, "hello.yaml")
      writeFileSync(graph, "apiVersion: openengine.cc/v1\nkind: Graph\nname: hello\nimplementation:\n  greet:\n    agent: claude\n    prompt: Say hello.\n")
      await terminal.run(`oe graph add ${graph}`)
      await terminal.run("oe repo untrust")
      await terminal.run('oe graph run hello "Say hello" --wait')
      expect(ours().length).toBeGreaterThan(0)
    } finally {
      await engine.stop()
    }
    expect(ours()).toEqual([])
  })

  test("stops and removes its directory when done", async () => {
    const engine = await startEngine()
    await engine.stop()
    await expect(fetch(`${engine.url}/api/config`)).rejects.toThrow()
    expect(existsSync(engine.root)).toBe(false)
  })
})
