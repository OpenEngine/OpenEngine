import { readFileSync, writeFileSync } from "node:fs"
import path from "node:path"
import type { Locator } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { anotherComputerCategory } from "./docCategories"
import { MAC_MINI, MAC_MINI_PATTERN } from "./macMini"

test.use({ terminalSession: { cols: 120, rows: 30 }, anotherComputerAt: MAC_MINI })

/** Which backend commands go to, and every backend OpenEngine knows, as its backends file records them. */
function backends(engine: Engine) {
  const file = JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
  return { current: file.current as string, names: Object.keys(file.backends ?? {}).sort() }
}

/** The graphs a daemon has saved, asked of it directly rather than through the CLI. */
async function graphNames(url: string, engine: Engine) {
  const response = await fetch(`${url}/api/v1/graphs`, {
    headers: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` },
  })
  expect(response.ok).toBe(true)
  return JSON.stringify(await response.json())
}

/**
 * A step the page shows, as a paragraph of its own: the generated Markdown
 * puts a step's text straight after whatever came before it, which would run
 * the intro into the first command, or a screenshot into the next step.
 */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

test.describe(anotherComputerCategory, () => {
  test.describe(
    withDocMeta("Use OpenEngine on Another Computer", {
      description: "Add another computer, such as a Mac mini, check you can reach it, switch to it, and use it for a single command.",
      sidebar_position: 1,
      slug: "/guides/other-computers/use-another-computer",
    }),
    () => {
      test("Add a Mac mini and choose which computer to use", async ({ engine, anotherComputer, terminal }, testInfo) => {
        /**
         * Screenshot the screen down to its last row in use, with `highlight`
         * labelled. The screenshots are committed and must come out the same on
         * every run, so the port the OS picked for this computer's daemon must
         * not show.
         */
        const show = async (filename: string, highlight: Locator, label: string) => {
          await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
          await screenshot(testInfo, highlight, {
            annotation: { filename, text: label },
            clip: await terminal.usedArea({ labelBelow: true }),
          })
        }
        const clear = () => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

        await test.step("[nodoc] The reader's project has a graph in hello.yaml", async () => {
          writeFileSync(
            path.join(engine.repository, "hello.yaml"),
            "apiVersion: openengine.cc/v1\nkind: Graph\nname: hello\nimplementation:\n  greet:\n    agent: claude\n    prompt: Reply with one sentence; don't run commands.\n",
          )
        })

        await step(
          `OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. It has a background program that does the work, and that program can run on more than one computer: this one, and another such as a Mac mini that stays on all day. This is so your long-running agents can work 24/7 on the tasks you set out for them, even after you close your laptop. Each computer it runs on is called a **backend**. In this example, your laptop is called \`local\`, and it's there from the start.

This guide shows how to tell OpenEngine about another computer, check you can reach it, and choose which computer your \`engine\` commands go to. You'll need OpenEngine already running on the other computer, and an address that reaches it from this one. Here, it's \`https://${MAC_MINI}\`.

OpenEngine on the other computer only answers programs on that computer itself, so it needs a safe way in from this one, such as a private network between your computers ([Tailscale](https://tailscale.com/kb/1017/install) is one) or an SSH tunnel. If someone else set up the Mac mini, ask them for the address that reaches it from here.`,
        )

        await step(
          `Give the other computer a name and tell OpenEngine its address. Type \`oe backend add mac-mini https://${MAC_MINI}\` and press Enter.

OpenEngine saves the Mac mini under the name \`mac-mini\` and shows what it saved: the name, the address, and a project setting you can leave alone. Your commands still go to this computer for now.`,
          async () => {
            await terminal.run(`oe backend add mac-mini https://${MAC_MINI}`)
            expect(backends(engine)).toEqual({ current: "local", names: ["local", "mac-mini"] })
            await show("another-computer__terminal--backend-added.png", await terminal.row(/^mac-mini\s+https:/), "The Mac mini is saved as mac-mini")
          },
        )

        await clear()

        await step(
          `Check that OpenEngine can reach it. Type \`oe backends list --check\` and press Enter.

You'll see every computer OpenEngine knows about, with *up* beside each one it could reach, followed by the agents it can use there. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you. The star marks the computer your commands go to: still \`local\`. You can leave the other columns alone for now.

If you see *down* beside the Mac mini instead, check that it's switched on, that OpenEngine is running on it, that the private network or tunnel to it is working, and that you typed its address correctly. If it says the Mac mini *requires a token*, it was given a token of its own: see [\`oe backend add\`](../../cli-reference.md#engine-backend-add) for how to send it.`,
          async () => {
            const { output } = await terminal.run("oe backends list --check")
            expect(output).toMatch(/^\*\s+local\s.*\bup\b/m)
            expect(output).toMatch(new RegExp(`^\\s+mac-mini\\s+https://${MAC_MINI_PATTERN}\\s.*\\bup\\b`, "m"))
            await show("another-computer__terminal--backends-checked.png", await terminal.row(/^\s+mac-mini\s.*\bup\b/), "The Mac mini is up: OpenEngine can reach it")
          },
        )

        await clear()

        await step(
          `Switch to the Mac mini, so your commands go there from now on. Type \`oe backend use mac-mini\` and press Enter.

OpenEngine shows the computer you switched to, with a star to say your commands go there now.`,
          async () => {
            await terminal.run("oe backend use mac-mini")
            expect(backends(engine).current).toBe("mac-mini")
            await show("another-computer__terminal--backend-selected.png", await terminal.row(/^\* mac-mini\s/), "Your commands now go to the Mac mini")
          },
        )

        await clear()

        await step(
          `Each computer keeps its own graphs: recipes for a piece of work, saying its steps, which agent does each one, and in what order. To see that your commands now go to the Mac mini, save a graph there. Here, your project has one in a file called \`hello.yaml\`. Type \`oe graph add hello.yaml\` and press Enter.

OpenEngine saves the graph, called \`hello\`, on the Mac mini. The screen says *registered*, OpenEngine's word for saved, followed by labels OpenEngine uses to tell graphs and their versions apart, and the project it's saved under. You can leave those alone.`,
          async () => {
            await terminal.run("oe graph add hello.yaml")
            expect(await graphNames(anotherComputer!.url, engine)).toContain('"hello"')
            expect(await graphNames(engine.url, engine)).not.toContain('"hello"')
            await show("another-computer__terminal--graph-saved.png", await terminal.row(/^registered hello\s/), "hello is saved on the Mac mini")
          },
        )

        await clear()

        await step(
          `Type \`oe graphs list\` and press Enter.

You'll see the Mac mini's graphs, with \`hello\` among them.`,
          async () => {
            const { output } = await terminal.run("oe graphs list")
            expect(output).toMatch(/^hello\s/m)
            await show("another-computer__terminal--remote-graphs.png", await terminal.row(/^hello\s/), "The graph you saved on the Mac mini")
          },
        )

        await clear()

        await step(
          `To use another computer for a single command without switching, add \`--backend\` and its name. Type \`oe graphs list --backend local\` and press Enter.

You'll see this computer's graphs instead, and \`hello\` isn't among them: you saved it on the Mac mini. Your other commands still go to the Mac mini.`,
          async () => {
            const { output } = await terminal.run("oe graphs list --backend local")
            expect(output).not.toMatch(/^hello\s/m)
            expect(backends(engine).current).toBe("mac-mini")
            await show("another-computer__terminal--local-graphs.png", await terminal.row(/^NAME\s+VERSION\s/), "This computer's graphs: no hello here")
          },
        )

        await clear()

        await step(
          `When you want your commands to go to this computer again, switch back the same way. Type \`oe backend use local\` and press Enter.

OpenEngine shows \`local\` with the star: your commands go to this computer again.`,
          async () => {
            await terminal.run("oe backend use local")
            expect(backends(engine)).toEqual({ current: "local", names: ["local", "mac-mini"] })
            await show("another-computer__terminal--local-selected.png", await terminal.row(/^\* local\s/), "Your commands go to this computer again")
          },
        )

        await step(
          "Next, write and save your first graph: see [Save Your First Graph](../test2doc-graphs/test2doc-save-your-first-graph.mdx).",
        )
      })
    },
  )
})
