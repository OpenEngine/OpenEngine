import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { test, expect, startEngine } from "../fixtures/engine.ts"

const EXAMPLE_GRAPH = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../docs/examples/graphs/dead-code.yaml")

test.describe("engine daemon fixture", { tag: ["@skip-docs"] }, () => {
  test("serves the CLI as `local`, so specs type plain engine commands", async ({ engine, terminal }) => {
    const { output } = await terminal.run("engine backends list --check")
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
    await terminal.run(`engine graph add ${EXAMPLE_GRAPH}`)
    expect((await terminal.run("engine graphs list")).output).toMatch(/^dead-code\s+1\s/m)
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

  test("stops and removes its directory when done", async () => {
    const engine = await startEngine()
    await engine.stop()
    await expect(fetch(`${engine.url}/api/config`)).rejects.toThrow()
    expect(existsSync(engine.root)).toBe(false)
  })
})
