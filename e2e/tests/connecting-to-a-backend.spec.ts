import { readFileSync, writeFileSync } from "node:fs"
import { createServer, request as httpRequest } from "node:http"
import type { AddressInfo } from "node:net"
import path from "node:path"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test as engineTest, expect, startEngine, type Engine } from "../fixtures/engine.ts"
import { backendsCategory } from "./docCategories"

// What the reader's daemons answer at: their own on the default port, and one
// on a Mac mini on the network.
const LOCAL_URL = "http://127.0.0.1:4364"
const MAC_MINI_URL = "http://mac-mini.local:4364"

type BackendsFile = { current: string; backends: Record<string, { url: string; token_env?: string }> }

function backendsFile(engine: Engine): BackendsFile {
  return JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
}

/**
 * An HTTP proxy sending each of `routes`' URLs to the daemon that stands in
 * for it, so the CLI, pointed at it with `http_proxy`, can use the URLs a
 * reader would rather than the test daemons' own ports.
 */
async function startProxy(routes: Record<string, string>) {
  const server = createServer((request, response) => {
    const asked = URL.canParse(request.url!) ? new URL(request.url!) : undefined
    const target = asked && routes[asked.origin]
    if (!target) {
      response.writeHead(502).end()
      return
    }
    const upstream = httpRequest(
      new URL(asked.pathname + asked.search, target),
      { method: request.method, headers: { ...request.headers, host: new URL(target).host } },
      (answer) => {
        response.writeHead(answer.statusCode!, answer.headers)
        answer.pipe(response)
      },
    )
    upstream.on("error", () => response.writeHead(502).end())
    request.pipe(upstream)
  })
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
  const { port } = server.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}`,
    stop: () => new Promise<void>((resolve) => server.close(() => resolve())),
  }
}

// A second daemon for the guide to connect to, standing in for one on another
// machine. The guide exports its token in the terminal, as a reader would.
const test = engineTest.extend<{ macMini: Engine; daemonProxy: { url: string } }>({
  macMini: async ({}, use) => {
    const macMini = await startEngine()
    try {
      await use(macMini)
    } finally {
      await macMini.stop()
    }
  },
  daemonProxy: async ({ engine, macMini }, use) => {
    const proxy = await startProxy({ [LOCAL_URL]: engine.url, [MAC_MINI_URL]: macMini.url })
    const file = backendsFile(engine)
    file.backends.local!.url = LOCAL_URL
    writeFileSync(engine.env.ENGINE_BACKENDS_FILE!, JSON.stringify(file, null, 2))
    try {
      await use(proxy)
    } finally {
      await proxy.stop()
    }
  },
  terminalEnv: async ({ engine, daemonProxy: proxy }, use) => {
    await use({ ...engine.env, http_proxy: proxy.url, HTTP_PROXY: proxy.url, no_proxy: "", NO_PROXY: "" })
  },
})

test.use({ terminalSession: { cols: 100, rows: 10 } })

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
          "A backend is a named engine daemon the CLI sends its commands to. `local`, the daemon on your own machine, is there from the start and selected by default. To add another, you need two things from it: its URL, `http://<its host>:4364` unless it was started on another port, and the `ENGINE_SERVICE_TOKEN` it was started with. Keep the token in an environment variable rather than typing it into commands: `export MAC_MINI_TOKEN=<the token>`.",
          async () => {
            await terminal.run(`export MAC_MINI_TOKEN=${macMini.env.ENGINE_SERVICE_TOKEN}`)
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Name the other daemon with `engine backend add`, giving it a name, its URL, and with `--token-env` the environment variable that holds its token: `engine backend add mac-mini http://mac-mini.local:4364 --token-env MAC_MINI_TOKEN`.",
          async () => {
            await terminal.run(`engine backend add mac-mini ${MAC_MINI_URL} --token-env MAC_MINI_TOKEN`)
            expect(backendsFile(engine).backends["mac-mini"]).toMatchObject({ url: MAC_MINI_URL, token_env: "MAC_MINI_TOKEN" })
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
            expect(output).toMatch(new RegExp(`^\\*\\s+local\\s+${escape(LOCAL_URL)}\\s.*\\bup\\b`, "m"))
            expect(output).toMatch(new RegExp(`^\\s+mac-mini\\s+${escape(MAC_MINI_URL)}\\s.*\\bup\\b`, "m"))
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "The new backend answered: it is up" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Run `engine backend use mac-mini` to send every command after it to that daemon. `engine backends list` now marks `mac-mini` with `*`.",
          async () => {
            await terminal.run("engine backend use mac-mini")
            expect(backendsFile(engine).current).toBe("mac-mini")
            await terminal.run("clear")
            expect((await terminal.run("engine backends list")).output).toMatch(/^\*\s+mac-mini\s/m)
            await screenshot(testInfo, await terminal.row(/^\*\s+mac-mini\s/), {
              annotation: { text: "Selected: commands go here" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Commands now go to `mac-mini`. Register a graph file from your project with `engine graph add`; the CLI reads it here and sends it to `mac-mini`. This guide uses one called `hello.yaml`, and adds `--pretty` for a one-line answer instead of JSON: `engine graph add hello.yaml --pretty`.",
          async () => {
            const { output } = await terminal.run("engine graph add hello.yaml --pretty")
            expect(output).toMatch(/^registered hello v/m)
            await screenshot(testInfo, await terminal.row(/^registered hello v/), {
              annotation: { text: "Registered on mac-mini" },
            })
            expect((await terminal.run("engine graphs list --json --backend mac-mini")).output).toContain('"hello"')
            expect((await terminal.run("engine graphs list --json --backend local")).output).not.toContain('"hello"')
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "To reach a different backend for one command only, add `--backend`. `engine graphs list --backend local` lists the graphs on `local`, which hasn't got `hello`, and leaves `mac-mini` selected: a plain `engine graphs list` after it still lists `mac-mini`'s graphs.",
          async () => {
            const { output } = await terminal.run("engine graphs list --backend local")
            expect(output).not.toMatch(/^hello\s/m)
            expect(backendsFile(engine).current).toBe("mac-mini")
            expect((await terminal.run("engine graphs list")).output).toMatch(/^hello\s/m)
            await screenshot(testInfo, await terminal.row(/^hello\s/), {
              annotation: { text: "Back on mac-mini without switching" },
            })
          },
        )
      })
    },
  )
})
