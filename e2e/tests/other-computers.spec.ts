import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { otherComputersCategory } from "./docCategories"

const MAC_MINI = "mac-mini.local:4364"

test.use({ terminalSession: { cols: 150, rows: 30 }, otherComputerAt: MAC_MINI })

/** Which backend commands go to, and the ones OpenEngine knows about, as the backends file records them. */
function chosen(engine: Engine) {
  const file = JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
  return { current: file.current as string, names: Object.keys(file.backends ?? {}) }
}

test.describe(otherComputersCategory, () => {
  test.describe(
    withDocMeta("Use OpenEngine on Another Computer", {
      description: "Add another computer, such as a Mac mini, check you can reach it, switch to it, and use it for a single command.",
      sidebar_position: 1,
      slug: "/guides/other-computers/use-another-computer",
    }),
    () => {
      test("Add a Mac mini and switch between computers", async ({ engine, otherComputer, terminal }, testInfo) => {
        await test.step("[nodoc] This computer has a graph saved; the Mac mini has none", async () => {
          const graph = path.join(engine.root, "hello.yaml")
          writeFileSync(
            graph,
            "apiVersion: openengine.cc/v1\nkind: Graph\nname: hello\nimplementation:\n  greet:\n    agent: claude\n    prompt: Reply with one sentence; don't run commands.\n",
          )
          await terminal.run(`engine graph add ${graph}`)
          await terminal.run("clear")
        })

        await test.step(
          `OpenEngine has AI coding agents work on your code, step by step, in the way you lay out. Its background program can run on more than one computer: this one, and another such as a Mac mini that stays on all day. Each computer it runs on is called a **backend**. The one on this computer is called \`local\`, and it's there from the start.

This guide shows how to tell OpenEngine about another computer, check you can reach it, and choose which computer your \`engine\` commands go to. You'll need OpenEngine running on the other computer already, and its address: here, \`http://${MAC_MINI}\`.`,
          async () => {},
        )

        await test.step(
          `Give the other computer a name and tell OpenEngine its address. Type \`engine backend add mac-mini http://${MAC_MINI}\` and press Enter.

OpenEngine remembers the Mac mini under the name \`mac-mini\`, and shows what it saved.`,
          async () => {
            await terminal.run(`engine backend add mac-mini http://${MAC_MINI}`)
            expect(chosen(engine)).toEqual({ current: "local", names: ["local", "mac-mini"] })
            await screenshot(testInfo, await terminal.row(/"name": "mac-mini"/), {
              annotation: { text: "The Mac mini is saved as mac-mini" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Check that OpenEngine can reach it. Type \`engine backends list --check\` and press Enter.

You'll see every computer OpenEngine knows about, with *up* beside each one it could reach. The star shows the one your commands go to now: still \`local\`.

If you see *down* beside the Mac mini instead, check that it's switched on, that OpenEngine is running on it, and that you typed its address correctly.`,
          async () => {
            const { output } = await terminal.run("engine backends list --check")
            expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
            expect(output).toMatch(new RegExp(`^\\s+mac-mini\\s+http://${MAC_MINI.replace(/\./g, "\\.")}\\s.*\\bup\\b`, "m"))
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "The Mac mini is reachable" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Switch to the Mac mini, so your commands go there from now on. Type \`engine backend use mac-mini\` and press Enter.

OpenEngine shows the computer you switched to.`,
          async () => {
            await terminal.run("engine backend use mac-mini")
            expect(chosen(engine).current).toBe("mac-mini")
            await screenshot(testInfo, await terminal.row(/"name": "mac-mini".*"current": true/), {
              annotation: { text: "Commands now go to the Mac mini" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Graphs are OpenEngine's recipes for a piece of work: its steps, which agent does each one, and in what order. Each computer keeps its own. Type \`engine graphs list\` and press Enter.

You'll see the Mac mini's graphs. It has none yet, so the list is empty, even though this computer has one.`,
          async () => {
            const { output } = await terminal.run("engine graphs list")
            expect(output).not.toMatch(/^hello\s/m)
            await screenshot(testInfo, terminal.screen(), {
              annotation: { text: "The Mac mini has no graphs yet" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `To use a different computer for a single command, without switching, add \`--backend\` and its name to the end. Type \`engine graphs list --backend local\` and press Enter.

You'll see this computer's graphs. Your other commands still go to the Mac mini.`,
          async () => {
            const { output } = await terminal.run("engine graphs list --backend local")
            expect(output).toMatch(/^hello\s/m)
            expect(chosen(engine).current).toBe("mac-mini")
            await screenshot(testInfo, await terminal.row(/^hello\s/), {
              annotation: { text: "A graph on this computer" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step("[nodoc] The Mac mini still has no graphs, and this computer still has its one", async () => {
          const graphs = async (url: string) =>
            (await (
              await fetch(`${url}/api/v1/graphs`, { headers: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` } })
            ).json()) as unknown
          expect(JSON.stringify(await graphs(engine.url))).toContain('"hello"')
          expect(JSON.stringify(await graphs(otherComputer!.url))).not.toContain('"hello"')
          await terminal.run("clear")
        })

        await test.step(
          `When you want your commands to go to this computer again, switch back the same way. Type \`engine backend use local\` and press Enter.`,
          async () => {
            await terminal.run("engine backend use local")
            expect(chosen(engine).current).toBe("local")
            await screenshot(testInfo, await terminal.row(/"name": "local".*"current": true/), {
              annotation: { text: "Commands go to this computer again" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step(
          "Next, to see everything else you can do with the `engine` command, read **Getting Help**.",
          async () => {},
        )
      })
    },
  )
})
