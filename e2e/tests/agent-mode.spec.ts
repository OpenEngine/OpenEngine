import { existsSync, readdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { test, expect, claudeModel, CHEAPEST_CLAUDE_MODEL, resolveAgentMode } from "../fixtures/engine.ts"

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

/** HELLO under another name, with its step asking for `model` (left out when undefined). */
function helloWith(name: string, model: string | undefined) {
  return HELLO.replace("name: hello", `name: ${name}`) + (model ? `    model: ${model}\n` : "")
}

/** Every model Claude Code's transcripts say answered in `workspace`. */
function transcriptModels(workspace: string) {
  const projects = path.join(process.env.CLAUDE_CONFIG_DIR ?? path.join(os.homedir(), ".claude"), "projects")
  const models = new Set<string>()
  for (const cwd of new Set([workspace, realpathSync(workspace)])) {
    const directory = path.join(projects, cwd.replace(/[^a-zA-Z0-9]/g, "-"))
    if (!existsSync(directory)) continue
    for (const file of readdirSync(directory).filter((name) => name.endsWith(".jsonl"))) {
      for (const line of readFileSync(path.join(directory, file), "utf8").split("\n")) {
        if (!line.trim()) continue
        const entry = JSON.parse(line)
        if (entry.type === "assistant" && entry.message?.model) models.add(entry.message.model)
      }
    }
  }
  return [...models]
}

test.describe("agent mode", { tag: ["@skip-docs"] }, () => {
  test("scripts the agents unless real ones are asked for", () => {
    expect(resolveAgentMode({})).toBe("stub")
    expect(resolveAgentMode({ E2E_AGENT: "stub" })).toBe("stub")
    expect(resolveAgentMode({ E2E_AGENT: "real" }, { claudeLogin: true, npx: true })).toBe("real")
    expect(() => resolveAgentMode({ E2E_AGENT: "sometimes" })).toThrow(/E2E_AGENT must be real or stub/)
  })

  test("real agents run the cheapest Claude model unless E2E_CLAUDE_MODEL names another", () => {
    expect(claudeModel({})).toBe(CHEAPEST_CLAUDE_MODEL)
    expect(claudeModel({ E2E_CLAUDE_MODEL: "claude-sonnet-5" })).toBe("claude-sonnet-5")
  })

  test("runs every model tier on the model real agents are given", async ({ engine, terminal }) => {
    test.skip(engine.agentMode !== "real", "only real agents run a model")
    test.setTimeout(600_000)
    expect(engine.daemonEnv.ANTHROPIC_MODEL).toBe(claudeModel())
    await terminal.run("engine repo untrust")
    for (const [name, model] of [["untiered", undefined], ["tier-default", "default"], ["tier-elevated", "elevated"]] as const) {
      const graph = path.join(engine.root, `${name}.yaml`)
      writeFileSync(graph, helloWith(name, model))
      await terminal.run(`engine graph add ${graph}`)
      await terminal.run(`engine graph run ${name} "Say hello" --wait`, { timeout: 180_000 })
      const listed = (await terminal.run("engine runs list")).output
      const runId = listed.match(new RegExp(`^(run-\\w+)\\s.*\\s${name} v1\\s+completed\\s`, "m"))?.[1]
      expect(runId, `${name} ran to completion`).toBeTruthy()
      const detail = JSON.parse((await terminal.run(`engine run get ${runId} --json`)).output)
      expect(transcriptModels(detail.workspace.path), `the models that answered ${name}`).toEqual([claudeModel()])
    }
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
    await terminal.run("engine repo untrust")
    await terminal.run('engine graph run hello "Say hello" --wait', { timeout: 280_000 })
    // State, never wording: the same assertion holds for a scripted agent and a real one.
    expect((await terminal.run("engine runs list")).output).toMatch(/^run-\w+\s.*\shello v1\s+completed\s/m)
  })

  test("the stub says what its script says", async ({ engine, terminal }) => {
    test.skip(engine.agentMode !== "stub", "only the stub agent is scripted")
    engine.script({ scenarios: [{ steps: [{ type: "say", text: "Scripted greeting 4c1e." }] }] })
    const graph = path.join(engine.root, "hello.yaml")
    writeFileSync(graph, HELLO)
    await terminal.run(`engine graph add ${graph}`)
    await terminal.run("engine repo untrust")
    const { output } = await terminal.run('engine graph run hello "Say hello" --wait')
    expect(output).toContain("Scripted greeting 4c1e.")
  })

  test("runs check out the fixture repository, never the one the tests live in", async ({ engine, terminal }) => {
    const repository = realpathSync(engine.repository)
    expect((await terminal.run("pwd -P")).output).toBe(repository)
    const graph = path.join(engine.root, "hello.yaml")
    writeFileSync(graph, HELLO)
    await terminal.run(`engine graph add ${graph}`)
    await terminal.run("engine repo untrust")
    await terminal.run('engine graph run hello "Say hello" --wait', { timeout: 280_000 })
    const run = JSON.parse((await terminal.run("engine runs list --json")).output).runs[0]
    const { output } = await terminal.run(`engine run get ${run.runId} --json`)
    const workspace = JSON.parse(output).workspace.path
    expect(readFileSync(path.join(workspace, ".git"), "utf8")).toContain(`${repository}/.git/worktrees/`)
  })

  test("checks out a repository with an origin, so runs need no --repo", async ({ engine }) => {
    expect(path.basename(engine.repository)).toBe("repository")
    expect(engine.daemonEnv.ENGINE_FAKE_SCRIPT === undefined).toBe(engine.agentMode === "real")
  })
})
