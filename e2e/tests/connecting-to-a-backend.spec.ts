import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import type { Page } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test as engineTest, engineFixture, expect, type Engine } from "../fixtures/engine.ts"
import type { Terminal } from "../fixtures/terminal.ts"
import { backendsCategory } from "./docCategories"

type BackendsFile = { current: string; backends: Record<string, { url: string; token_env?: string }> }

/** What `engine backend ...` wrote: the state a guide step changes, rather than what the CLI printed. */
function backendsFile(engine: Engine): BackendsFile {
  return JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
}

/** The graphs a backend holds, by name, read from the CLI's JSON rather than its table. */
async function graphNames(terminal: Terminal, command: string) {
  const { output } = await terminal.run(`${command} --json`)
  const { graphs } = JSON.parse(output.trim().split("\n").pop()!) as { graphs: { name: string }[] }
  return graphs.map((graph) => graph.name)
}

/** The daemons listen on ports the OS picks, so a committed guide must never draw them. */
async function expectNoRealPorts(page: Page, engines: Engine[]) {
  const drawn = (await page.locator(".xterm-rows").textContent()) ?? ""
  for (const engine of engines) expect(drawn).not.toContain(`:${new URL(engine.url).port}`)
}

// A second daemon for the guide to connect to, standing in for one on another
// machine on your network.
const test = engineTest.extend<{ macMini: Engine }>({
  macMini: async ({}, use, testInfo) => {
    await engineFixture(use, testInfo, "mac-mini")
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
      description:
        "How to name another engine daemon, check it is up, switch to it, and reach a different one for a single command.",
      sidebar_position: 1,
      slug: "/guides/backends/connecting-to-a-backend",
    }),
    () => {
      test("How to connect the CLI to another engine daemon", async ({ engine, macMini, terminal, page }, testInfo) => {
        await test.step("[nodoc] Put a tiny graph in the project", async () => {
          writeFileSync(path.join(engine.repository, "hello.yaml"), HELLO_GRAPH)
        })

        await test.step(
          "A backend is a named engine daemon the CLI sends its commands to. `local`, the daemon on your own machine, is there from the start and selected. To add another you need its URL and the `ENGINE_SERVICE_TOKEN` it was started with. Keep the token in an environment variable rather than in commands: `export MAC_MINI_TOKEN=<the token>`.",
          async () => {
            await terminal.run(`export MAC_MINI_TOKEN=${macMini.env.ENGINE_SERVICE_TOKEN}`)
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backend add` with a name for the daemon, its URL, and, with `--token-env`, the variable holding its token: `engine backend add mac-mini http://<its host>:4364 --token-env MAC_MINI_TOKEN --pretty`. (`--pretty` prints a line to read rather than JSON for a script; both daemons in this guide run on `127.0.0.1`.)",
          async () => {
            await terminal.run(`engine backend add mac-mini ${macMini.url} --token-env MAC_MINI_TOKEN --pretty`)
            expect(backendsFile(engine).backends["mac-mini"]).toMatchObject({
              url: macMini.url,
              token_env: "MAC_MINI_TOKEN",
            })
            // Adding a backend doesn't select it.
            expect(backendsFile(engine).current).toBe("local")
            await expectNoRealPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.row(/^mac-mini\s/), {
              annotation: { text: "The new backend, by name" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backends list --check` to see every backend, which one is selected (`*`), and whether each one is up.",
          async () => {
            const { output } = await terminal.run("engine backends list --check")
            expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
            expect(output).toMatch(/^\s+mac-mini\s.*\bup\b/m)
            await expectNoRealPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "mac-mini answered: it is up" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backend use mac-mini --pretty` to send every command after it to that daemon. `engine backends list` now marks `mac-mini` with `*`.",
          async () => {
            await terminal.run("engine backend use mac-mini --pretty")
            expect(backendsFile(engine).current).toBe("mac-mini")
            await terminal.run("clear")
            expect((await terminal.run("engine backends list")).output).toMatch(/^\*\s+mac-mini\s/m)
            await expectNoRealPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.row(/^\*\s+mac-mini\s/), {
              annotation: { text: "Selected: commands go here" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Commands now go to `mac-mini`. Register a graph there with `engine graph add FILE --pretty`, where `FILE` is any graph file in your project; here it is `hello.yaml`, a graph with one node.",
          async () => {
            await terminal.run("engine graph add hello.yaml --pretty")
            expect(await graphNames(terminal, "engine graphs list --backend mac-mini")).toContain("hello")
            expect(await graphNames(terminal, "engine graphs list --backend local")).not.toContain("hello")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "To reach a different backend for one command only, add `--backend NAME` to it. `engine graphs list` lists `hello` on `mac-mini`; `engine graphs list --backend local` lists the graphs on `local`, which has none, and leaves `mac-mini` selected.",
          async () => {
            expect((await terminal.run("engine graphs list")).output).toMatch(/^hello\s/m)
            const { output } = await terminal.run("engine graphs list --backend local")
            expect(output).not.toMatch(/^hello\s/m)
            expect(backendsFile(engine).current).toBe("mac-mini")
            await expectNoRealPorts(page, [engine, macMini])
            await screenshot(testInfo, await terminal.rows(/engine graphs list --backend local$/, /^NAME\s/), {
              annotation: { text: "Just this command went to local" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "`ENGINE_BACKEND=NAME` does the same for every command in a shell or script. `--backend` beats `ENGINE_BACKEND`, which beats the backend chosen with `engine backend use`.",
          async () => {
            expect(await graphNames(terminal, "ENGINE_BACKEND=local engine graphs list")).not.toContain("hello")
            expect(await graphNames(terminal, "ENGINE_BACKEND=local engine graphs list --backend mac-mini")).toContain(
              "hello",
            )
            expect(backendsFile(engine).current).toBe("mac-mini")
          },
        )
      })
    },
  )
})
