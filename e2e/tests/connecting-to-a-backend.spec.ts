import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import type { Page } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test as engineTest, expect, startEngine, type Engine } from "../fixtures/engine.ts"
import { backendsCategory } from "./docCategories"

type BackendsFile = { current: string; backends: Record<string, { url: string; token_env?: string }> }

function backendsFile(engine: Engine): BackendsFile {
  return JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
}

/** The graphs a backend holds, by name, from the CLI's JSON rather than its table. */
async function graphNames(terminal: { run(command: string): Promise<{ output: string }> }, backend: string) {
  const { output } = await terminal.run(`engine graphs list --json --backend ${backend}`)
  const listed = JSON.parse(output.trim().split("\n").pop()!) as { name: string }[] | { graphs: { name: string }[] }
  return (Array.isArray(listed) ? listed : listed.graphs).map((graph) => graph.name)
}

/** The daemons' ports change on every run, so a committed guide must never draw them. */
async function expectNoRandomPorts(page: Page, engines: Engine[]) {
  const drawn = (await page.locator(".xterm-rows").textContent()) ?? ""
  for (const engine of engines) expect(drawn).not.toContain(`:${new URL(engine.url).port}`)
}

// A second daemon for the guide to connect to, standing in for one on another
// machine on your network.
const test = engineTest.extend<{ macMini: Engine }>({
  macMini: async ({}, use) => {
    const macMini = await startEngine()
    try {
      await use(macMini)
    } finally {
      await macMini.stop()
    }
  },
})

test.use({ terminalSession: { cols: 100, rows: 10 } })

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
      test("How to connect the CLI to another engine daemon", async ({ engine, macMini, terminal, page }, testInfo) => {
        await test.step("[nodoc] Put a tiny graph in the project", async () => {
          writeFileSync(path.join(engine.repository, "hello.yaml"), HELLO_GRAPH)
        })

        await test.step(
          "A backend is a named engine daemon the CLI sends its commands to. `local`, the daemon on your own machine, is there from the start and selected. To add another, you need its URL and the `ENGINE_SERVICE_TOKEN` it was started with. Keep the token in an environment variable rather than typing it into commands: `export MAC_MINI_TOKEN=<the token>`.",
          async () => {
            await terminal.run(`export MAC_MINI_TOKEN=${macMini.env.ENGINE_SERVICE_TOKEN}`)
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backend add` with a name for the daemon, its URL, and with `--token-env` the variable holding its token: `engine backend add mac-mini http://<its host>:4364 --token-env MAC_MINI_TOKEN`.",
          async () => {
            await terminal.run(`engine backend add mac-mini ${macMini.url} --token-env MAC_MINI_TOKEN --pretty`)
            expect(backendsFile(engine).backends["mac-mini"]).toMatchObject({ url: macMini.url, token_env: "MAC_MINI_TOKEN" })
            expect(backendsFile(engine).current).toBe("local")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backends list --check` to see every backend, which one is selected (`*`), and whether each one is up. (In this guide both daemons run on the same machine, so both URLs point at `127.0.0.1`; yours shows the other machine's host and port 4364.)",
          async () => {
            const { output } = await terminal.run("engine backends list --check")
            expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
            expect(output).toMatch(/^\s+mac-mini\s.*\bup\b/m)
            await expectNoRandomPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "mac-mini answered: it is up" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backend use mac-mini` to send every command after it to that daemon. `engine backends list` now marks `mac-mini` with `*`.",
          async () => {
            await terminal.run("engine backend use mac-mini --pretty")
            expect(backendsFile(engine).current).toBe("mac-mini")
            await terminal.run("clear")
            expect((await terminal.run("engine backends list")).output).toMatch(/^\*\s+mac-mini\s/m)
            await expectNoRandomPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.row(/^\*\s+mac-mini\s/), {
              annotation: { text: "Selected: commands go here" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Commands now go to `mac-mini`. Register any graph file from your project there with `engine graph add`; here it is `hello.yaml`, a graph named `hello`. Add `--pretty` for a line of text rather than JSON.",
          async () => {
            await terminal.run("engine graph add hello.yaml --pretty")
            await screenshot(testInfo, await terminal.row(/^registered hello v1\b/), {
              annotation: { text: "Registered on mac-mini" },
            })
          },
        )

        await test.step("[nodoc] The graph is on mac-mini, not on local", async () => {
          expect(await graphNames(terminal, "mac-mini")).toContain("hello")
          expect(await graphNames(terminal, "local")).not.toContain("hello")
        })

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "To reach a different backend for one command only, add `--backend NAME` to it. `engine graphs list --backend local` lists the graphs on `local`, which has no `hello`. A plain `engine graphs list` right after it still goes to `mac-mini`, which stays selected.",
          async () => {
            const local = await terminal.run("engine graphs list --backend local")
            expect(local.output).not.toMatch(/^hello\s/m)
            expect(backendsFile(engine).current).toBe("mac-mini")
            expect((await terminal.run("engine graphs list")).output).toMatch(/^hello\s/m)
            await screenshot(testInfo, await terminal.row(/^hello\s/), {
              annotation: { text: "Without --backend: mac-mini again" },
            })
          },
        )
      })
    },
  )
})
