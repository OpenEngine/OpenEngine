import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { HELLO } from "../fixtures/graphs.ts"
import { otherComputersCategory } from "./docCategories"

const MAC_MINI = "mac-mini.local:4364"
/** Where a reader finds how to have OpenEngine accept connections from other computers. */
const SERVER_SETTINGS = "https://github.com/OpenEngine/OpenEngine/blob/main/docs/releases.md#manual-install"

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
        await test.step("[nodoc] This computer has a graph saved, so it's clear which computer a command went to", async () => {
          const graph = path.join(engine.root, "hello.yaml")
          writeFileSync(graph, HELLO)
          await terminal.run(`engine graph add ${graph}`)
          await terminal.run("clear")
        })

        await test.step(
          `OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. (An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you.) Its background program can run on more than one computer: this one, and another such as a Mac mini that stays on all day. Each computer it runs on is called a **backend**. The one on this computer is called \`local\`, and it's there from the start.

This guide shows how to tell OpenEngine about another computer, check you can reach it, use it for a single command, and choose which computer your \`engine\` commands go to.

You'll need OpenEngine running on the other computer already, and its address: here, \`http://${MAC_MINI}\`. When OpenEngine is first installed, it only answers the computer it runs on, so the Mac mini also needs to be set up to accept connections from other computers. The [release notes](${SERVER_SETTINGS}) say how; ask whoever looks after the Mac mini if you're not sure.
`,
          async () => {},
        )

        await test.step(
          `Give the other computer a name and tell OpenEngine its address. Type \`engine backend add mac-mini http://${MAC_MINI} --pretty\` and press Enter. (\`--pretty\` asks for a short line of text rather than the full details.)

OpenEngine remembers the Mac mini under the name \`mac-mini\`, and shows its name and address. *project default* is the group its graphs are kept in; you can leave that as it is.

Your commands reach the Mac mini over your network without being encrypted, so only use an \`http://\` address like this on a home or office network you trust.`,
          async () => {
            await terminal.run(`engine backend add mac-mini http://${MAC_MINI} --pretty`)
            expect(chosen(engine)).toEqual({ current: "local", names: ["local", "mac-mini"] })
            await screenshot(testInfo, await terminal.row(/^mac-mini\s+http:/), {
              annotation: { text: "The Mac mini is saved as mac-mini" },
              clip: await terminal.usedArea({ rowsBelow: 2 }),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Check that OpenEngine can reach it. Type \`engine backends list --check\` and press Enter.

You'll see every computer OpenEngine knows about, with *up* beside each one it could reach. The names in brackets after *up* are the agents that computer can use. The star shows the one your commands go to now: still \`local\`. The PROJECT and TOKEN columns are settings you won't need here.

If you see *down* beside the Mac mini instead, check that it's switched on, that OpenEngine is running on it and set to accept connections from other computers, and that you typed its address correctly.`,
          async () => {
            const { output } = await terminal.run("engine backends list --check")
            expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
            expect(output).toMatch(new RegExp(`^\\s+mac-mini\\s+http://${MAC_MINI.replace(/\./g, "\\.")}\\s.*\\bup\\b`, "m"))
            await screenshot(testInfo, await terminal.row(/^\s+mac-mini\s.*\bup\b/), {
              annotation: { text: "The Mac mini is reachable" },
              clip: await terminal.usedArea({ rowsBelow: 2 }),
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Graphs are OpenEngine's recipes for a piece of work: its steps, which agent does each one, and in what order. Each computer keeps its own. To use the Mac mini for a single command, without switching to it, add \`--backend\` and its name to the end. Type \`engine graphs list --backend mac-mini\` and press Enter.

You'll see the graphs saved on the Mac mini. If it has none yet, as here, there are only the column headings. Your other commands still go to this computer.`,
          async () => {
            const { output } = await terminal.run("engine graphs list --backend mac-mini")
            expect(output).not.toMatch(/^hello\s/m)
            expect(chosen(engine).current).toBe("local")
            await screenshot(testInfo, await terminal.row(/^NAME\s+VERSION\s/), {
              annotation: { text: "The Mac mini's graphs: none yet" },
              clip: await terminal.usedArea({ rowsBelow: 2 }),
            })
          },
        )

        await test.step("[nodoc] Without --backend, commands still go to this computer", async () => {
          await terminal.run("clear")
          expect((await terminal.run("engine graphs list")).output).toMatch(/^hello\s/m)
          await terminal.run("clear")
        })

        await test.step(
          `If you'll be using the Mac mini for a while, switch to it, so your commands go there from now on without \`--backend\`. Type \`engine backend use mac-mini --pretty\` and press Enter.

OpenEngine shows the computer you switched to, with a star to say your commands go there now.`,
          async () => {
            await terminal.run("engine backend use mac-mini --pretty")
            expect(chosen(engine).current).toBe("mac-mini")
            await screenshot(testInfo, await terminal.row(/^\* mac-mini\s+http:/), {
              annotation: { text: "Commands now go to the Mac mini" },
              clip: await terminal.usedArea({ rowsBelow: 2 }),
            })
          },
        )

        await test.step("[nodoc] Commands now go to the Mac mini, and this computer still has its graph", async () => {
          await terminal.run("clear")
          expect((await terminal.run("engine graphs list")).output).not.toMatch(/^hello\s/m)
          const graphs = async (url: string) =>
            (await (
              await fetch(`${url}/api/v1/graphs`, { headers: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` } })
            ).json()) as unknown
          expect(JSON.stringify(await graphs(engine.url))).toContain('"hello"')
          expect(JSON.stringify(await graphs(otherComputer!.url))).not.toContain('"hello"')
          await terminal.run("clear")
        })

        await test.step(
          `When you want your commands to go to this computer again, switch back the same way. Type \`engine backend use local --pretty\` and press Enter.`,
          async () => {
            await terminal.run("engine backend use local --pretty")
            expect(chosen(engine).current).toBe("local")
            await screenshot(testInfo, await terminal.row(/^\* local\s+http:/), {
              annotation: { text: "Commands go to this computer again" },
              clip: await terminal.usedArea({ rowsBelow: 2 }),
            })
          },
        )

        await test.step(
          "Every `engine` command works the same way on the Mac mini as here. To see what else you can do with backends, type `engine backend --help`, the way [Getting Help](/docs/guides/getting-started/getting-help) shows.",
          async () => {},
        )
      })
    },
  )
})
