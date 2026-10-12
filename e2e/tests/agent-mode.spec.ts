import { realpathSync, writeFileSync } from "node:fs"
import path from "node:path"
import { test, expect, resolveAgentMode } from "../fixtures/engine.ts"

/** A one-step graph any agent can finish quickly, and nothing it does can be wrong. */
const HELLO = `apiVersion: openengine.cc/v1
kind: Graph
name: hello
description: Greet the reader.
implementation:
  greet:
    agent: claude
    prompt: Reply with one short, friendly sentence greeting the reader. Do not run commands or edit files.
`

test.describe("agent mode", { tag: ["@skip-docs"] }, () => {
  test("scripts the agents unless real ones are asked for", () => {
    expect(resolveAgentMode({})).toBe("stub")
    expect(resolveAgentMode({ E2E_AGENT: "stub" })).toBe("stub")
    expect(resolveAgentMode({ E2E_AGENT: "real" }, { claudeLogin: true, npx: true })).toBe("real")
    expect(() => resolveAgentMode({ E2E_AGENT: "sometimes" })).toThrow(/E2E_AGENT must be real or stub/)
  })

  test("refuses real agents where none can run, rather than quietly scripting them", () => {
    expect(() => resolveAgentMode({ E2E_AGENT: "real" }, { claudeLogin: false, npx: true })).toThrow(/Claude Code login/)
    expect(() => resolveAgentMode({ E2E_AGENT: "real" }, { claudeLogin: true, npx: false })).toThrow(/npx/)
  })

  test("runs a graph to completion on whichever agent is in use", async ({ engine, terminal }) => {
    test.setTimeout(engine.agentMode === "real" ? 300_000 : 60_000)
    engine.script({ scenarios: [{ steps: [{ type: "say", text: "Hello, and welcome to OpenEngine." }] }] })
    const graph = path.join(engine.root, "hello.yaml")
    writeFileSync(graph, HELLO)
    await terminal.run(`engine graph add ${graph}`)
    await terminal.run('engine graph run hello "Say hello" --wait', { timeout: 280_000 })
    // State, never wording: the same assertion holds for a scripted agent and a real one.
    expect((await terminal.run("engine runs list")).output).toMatch(/^run-\w+\s+hello v1\s+completed\s/m)
  })

  test("the stub says what its script says", async ({ engine, terminal }) => {
    test.skip(engine.agentMode !== "stub", "only the stub agent is scripted")
    engine.script({ scenarios: [{ steps: [{ type: "say", text: "Scripted greeting 4c1e." }] }] })
    const graph = path.join(engine.root, "hello.yaml")
    writeFileSync(graph, HELLO)
    await terminal.run(`engine graph add ${graph}`)
    const { output } = await terminal.run('engine graph run hello "Say hello" --wait --pretty')
    expect(output).toContain("Scripted greeting 4c1e.")
  })

  test("runs check out the fixture repository, never the one the tests live in", async ({ engine, terminal }) => {
    const repository = realpathSync(engine.repository)
    expect((await terminal.run("pwd -P")).output).toBe(repository)
    const graph = path.join(engine.root, "hello.yaml")
    writeFileSync(graph, HELLO)
    await terminal.run(`engine graph add ${graph}`)
    await terminal.run('engine graph run hello "Say hello" --wait', { timeout: 280_000 })
    const run = JSON.parse((await terminal.run("engine runs list --json")).output).runs[0]
    const { output } = await terminal.run(`engine run get ${run.runId}`)
    expect(output).toContain(`Checking ${repository} out`)
  })

  test("checks out a repository with an origin, so runs need no --repo", async ({ engine }) => {
    expect(path.basename(engine.repository)).toBe("repository")
    expect(engine.daemonEnv.ENGINE_FAKE_SCRIPT === undefined).toBe(engine.agentMode === "real")
  })
})
