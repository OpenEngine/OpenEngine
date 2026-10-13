import { execFileSync } from "node:child_process"
import { mkdtempSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import type { Locator } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { engineBin } from "../support/engine.ts"
import { graphsCategory } from "./docCategories"

test.use({ terminalSession: { cols: 120, rows: 40 } })

/** The graph this guide writes, before and after the reader changes its prompt. */
const FIRST = `apiVersion: openengine.cc/v1
kind: Graph
name: describe-project
implementation:
  describe:
    agent: claude
    prompt: Read README.md and reply with one sentence saying what this project is. Don't run commands or change files.
`
const CHANGED = FIRST.replace("one sentence", "two sentences")

/** The versions a daemon has saved of `name`, asked of it directly rather than through the CLI. */
async function versions(engine: Engine, name: string): Promise<number[]> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const listed = await fetch(`${engine.url}/api/v1/graphs`, { headers })
  expect(listed.ok).toBe(true)
  const graph = ((await listed.json()).graphs as { name: string; project: string }[]).find((g) => g.name === name)
  if (!graph) return []
  const response = await fetch(`${engine.url}/api/v1/graphs/${name}?project=${encodeURIComponent(graph.project)}`, { headers })
  expect(response.ok).toBe(true)
  return ((await response.json()).versions as { version: number }[]).map((v) => v.version).sort()
}

/** A step the page shows as a paragraph of its own (see another-computer.spec.ts). */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

test.describe(graphsCategory, () => {
  test.describe(
    withDocMeta("Save Your First Graph", {
      description: "Write down a piece of work as a graph, save it to OpenEngine, find it again, and see what happens when you change it.",
      sidebar_position: 1,
      slug: "/guides/graphs/save-your-first-graph",
    }),
    () => {
      test("Write a graph, save it, and change it", async ({ engine, terminal }, testInfo) => {
        const file = path.join(engine.repository, "describe-project.yaml")

        /** Screenshot the screen down to its last row in use, with `highlight` labelled. */
        const show = async (highlight: Locator, label: string, position?: "right") => {
          await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
          await screenshot(testInfo, highlight, {
            annotation: { text: label, ...(position ? { position } : {}) },
            clip: await terminal.usedArea({ labelBelow: true }),
          })
        }
        const clear = () => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

        await step(
          `OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you.

The way you lay out the work is a **graph**: a recipe for a piece of work, saying its steps, which agent does each one, and in what order. You write it once in a small text file and save it to OpenEngine, so you can find it and use it again whenever you need that piece of work done, instead of explaining it from scratch each time.

This guide shows how to write a graph, save it, find it again, and what happens when you change it. Running a graph comes in the next guide.`,
        )

        await step(
          `Every graph file follows the same layout. To see it, with an example, type \`engine graph spec | head -n 30\` and press Enter. \`engine graph spec\` describes everything a graph file can say, which is long; \`| head -n 30\` shows only its first 30 lines, where the example is.

You'll see a short example graph. It has more in it than a first graph needs: the next step shows the few lines yours needs. This works even when OpenEngine isn't running, so you can write a graph anywhere.`,
          async () => {
            const { output } = await terminal.run("engine graph spec | head -n 30")
            expect(output).toContain("apiVersion: openengine.cc/v1")
            await show(await terminal.rows(/^apiVersion: openengine\.cc\/v1$/, /^\s+prompt: "Review/), "An example graph")
          },
        )

        await test.step("[nodoc] engine graph spec needs no backend", async () => {
          // A backend nothing answers at: the spec must still print.
          const directory = mkdtempSync(path.join(os.tmpdir(), "engine-e2e-nobackend-"))
          try {
            const backendsFile = path.join(directory, "backends.json")
            writeFileSync(backendsFile, JSON.stringify({ current: "local", backends: { local: { url: "http://127.0.0.1:9" } } }))
            const output = execFileSync(engineBin, ["graph", "spec"], {
              env: { PATH: process.env.PATH!, HOME: directory, ENGINE_BACKENDS_FILE: backendsFile },
              encoding: "utf8",
            })
            expect(output).toContain("apiVersion: openengine.cc/v1")
          } finally {
            rmSync(directory, { recursive: true, force: true })
          }
        })

        await clear()

        await step(
          `Write your graph. In your project's folder, make a file called \`describe-project.yaml\` with any text editor, and copy this into it:

\`\`\`yaml
${FIRST}\`\`\`

The first two lines say this is an OpenEngine graph. \`name\` is what you'll call it later. Under \`implementation\` is its one step, \`describe\`: the agent that does it, here Claude Code, and the \`prompt\`, which is what you ask that agent to do, in plain words.

Keep the spaces at the start of each line as they are: they show which lines belong to which.`,
          async () => {
            writeFileSync(file, FIRST)
          },
        )

        await step(
          `Save the graph to OpenEngine. Type \`engine graph add describe-project.yaml\` and press Enter.

The screen says *registered*, OpenEngine's word for saved, then the graph's name and \`v1\`: this is its first version. The labels in brackets are how OpenEngine tells graphs and their versions apart; you can leave them alone.

If you see *cannot reach backend*, OpenEngine isn't running on this computer: start it and try again. If OpenEngine finds a mistake in the file, it lists what's wrong and where in the file; fix it and run the command again.`,
          async () => {
            await terminal.run("engine graph add describe-project.yaml")
            expect(await versions(engine, "describe-project")).toEqual([1])
            await show(await terminal.row(/^registered describe-project v1\s/), "Your graph is saved, as version 1")
          },
        )

        await clear()

        await step(
          `Later, to find the graphs you've saved, type \`engine graphs list\` and press Enter.

You'll see one row for each graph, with its name and latest version. You can leave the other columns alone.`,
          async () => {
            const { output } = await terminal.run("engine graphs list")
            expect(output).toMatch(/^describe-project\s+1\s/m)
            await show(await terminal.row(/^describe-project\s+1\s/), "The graph you saved")
          },
        )

        await clear()

        await step(
          `To see what a saved graph says, type \`engine graph get describe-project\` and press Enter.

You'll see the graph's name and version, the versions OpenEngine has of it, and then the file exactly as you saved it.`,
          async () => {
            const { output } = await terminal.run("engine graph get describe-project")
            expect(output).toContain("prompt: Read README.md and reply with one sentence")
            expect(output).not.toMatch(/^\s*[{[]/m)
            await show(await terminal.rows(/^apiVersion:/, /^\s+prompt:/), "The graph as you saved it")
          },
        )

        await clear()

        await step(
          `Now change the graph. In \`describe-project.yaml\`, change *one sentence* in the prompt to *two sentences*, and save the file. Then type \`engine graph add describe-project.yaml\` and press Enter again.

This time the screen says \`v2\`: OpenEngine saved your change as a new version. It keeps the old one too, so a change never overwrites what you had.`,
          async () => {
            writeFileSync(file, CHANGED)
            await terminal.run("engine graph add describe-project.yaml")
            expect(await versions(engine, "describe-project")).toEqual([1, 2])
            await show(await terminal.row(/^registered describe-project v2\s/), "Your change is saved as version 2")
          },
        )

        await clear()

        await step(
          `Type \`engine graph get describe-project\` and press Enter.

The line starting *versions* lists both versions, and below it is the file as it is now, with *two sentences*. When you use a graph by its name, OpenEngine uses its latest version.`,
          async () => {
            const { output } = await terminal.run("engine graph get describe-project")
            expect(output).toContain("two sentences")
            await show(await terminal.row(/^versions: v1 .*, v2 /), "Both versions are kept", "right")
          },
        )

        await clear()

        await step(
          `If you save a graph again without changing it, nothing new is made. Type \`engine graph add describe-project.yaml\` and press Enter once more, without touching the file.

The screen says *unchanged* and \`v2\`: OpenEngine already has this exact graph, so it keeps the version it had. You can save as often as you like; only a real change makes a new version.`,
          async () => {
            await terminal.run("engine graph add describe-project.yaml")
            expect(await versions(engine, "describe-project")).toEqual([1, 2])
            await show(await terminal.row(/^unchanged describe-project v2\s/), "Nothing changed, so no new version")
          },
        )

        await step(
          "Next, to learn everything else a graph file can say, such as more steps or one agent checking another's work, see [Graphs](../../graphs.md).",
        )
      })
    },
  )
})
