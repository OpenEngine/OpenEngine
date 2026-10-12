import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test as engineTest, expect, startEngine, type Engine } from "../fixtures/engine.ts"
import { backendsCategory } from "./docCategories"

// A second daemon for the guide to connect to, standing in for one on another
// machine. Its token reaches the terminal as MAC_MINI_TOKEN, as a reader would
// keep it in their shell's environment.
const test = engineTest.extend<{ macMini: Engine }>({
  macMini: async ({}, use) => {
    const macMini = await startEngine()
    try {
      await use(macMini)
    } finally {
      await macMini.stop()
    }
  },
  terminalEnv: async ({ engine, macMini }, use) => {
    await use({ ...engine.env, MAC_MINI_TOKEN: macMini.env.ENGINE_SERVICE_TOKEN! })
  },
})

test.use({ terminalSession: { cols: 100, rows: 10 } })

type BackendsFile = { current: string; backends: Record<string, { url: string; token_env?: string }> }

function backendsFile(engine: Engine): BackendsFile {
  return JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
}

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

const HELLO_GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: hello
plan:
  greet:
    agent: claude
    prompt: Reply with one sentence; don't run commands.
`

test.describe(backendsCategory, () => {
  test.describe(
    withDocMeta("Connecting to a Backend", {
      description: "How to name another engine daemon, check it is up, switch to it, and reach a different one for a single command.",
      sidebar_position: 1,
      slug: "/guides/backends/connecting-to-a-backend",
    }),
    () => {
      test("How to connect the CLI to another engine daemon", async ({ engine, macMini, terminal }, testInfo) => {
        await test.step("[nodoc] Put a tiny graph in the project", async () => {
          writeFileSync(path.join(engine.repository, "hello.yaml"), HELLO_GRAPH)
        })

        await test.step(
          "Name the other daemon with `engine backend add`: give it a name, its URL, and the environment variable that holds its token.",
          async () => {
            await terminal.run(`engine backend add mac-mini ${macMini.url} --token-env MAC_MINI_TOKEN`)
            expect(backendsFile(engine).backends["mac-mini"]).toMatchObject({ url: macMini.url, token_env: "MAC_MINI_TOKEN" })
            expect(backendsFile(engine).current).toBe("local")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backends list --check` to see every backend, which one is selected (`*`), and whether each is up.",
          async () => {
            const { output } = await terminal.run("engine backends list --check")
            expect(output).toMatch(new RegExp(`^\\*\\s+local\\s+${escape(engine.url)}\\s.*\\bup\\b`, "m"))
            expect(output).toMatch(new RegExp(`^\\s+mac-mini\\s+${escape(macMini.url)}\\s.*\\bup\\b`, "m"))
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "The new backend answered: it is up" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step("Run `engine backend use mac-mini` to send every command after it to that daemon.", async () => {
          await terminal.run("engine backend use mac-mini")
          expect(backendsFile(engine).current).toBe("mac-mini")
          expect((await terminal.run("engine backends list")).output).toMatch(/^\*\s+mac-mini\s/m)
        })

        await test.step("Commands now go to `mac-mini`. Register a graph there with `engine graph add hello.yaml`.", async () => {
          await terminal.run("engine graph add hello.yaml")
          expect((await terminal.run("engine graphs list --json --backend mac-mini")).output).toContain('"hello"')
          expect((await terminal.run("engine graphs list --json --backend local")).output).not.toContain('"hello"')
        })

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "To reach a different backend for one command only, add `--backend`. `engine graphs list --backend local` lists the graphs on `local`, and leaves `mac-mini` selected.",
          async () => {
            const { output } = await terminal.run("engine graphs list --backend local")
            expect(output).not.toMatch(/^hello\s/m)
            expect(backendsFile(engine).current).toBe("mac-mini")
            expect((await terminal.run("engine graphs list")).output).toMatch(/^hello\s/m)
          },
        )
      })
    },
  )
})
