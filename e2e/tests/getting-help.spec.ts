import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect } from "../fixtures/terminal.ts"
import { gettingStartedCategory } from "./docCategories"

test.use({ terminalSession: { cols: 100, rows: 38 } })

test.describe(gettingStartedCategory, () => {
  test.describe(
    withDocMeta("Getting Help", {
      description: "How to see everything the engine command can do, and get help with any part of it.",
      sidebar_position: 2,
      slug: "/guides/getting-started/getting-help",
    }),
    () => {
      test("See what you can do", async ({ terminal }, testInfo) => {
        await test.step(
          `Every OpenEngine command starts with \`engine\`. To see all of them, type \`engine --help\` and press Enter.

You'll see a list of commands, each with a short note on what it does.`,
          async () => {
            const { output } = await terminal.run("engine --help")
            expect(output).toMatch(/^usage: engine/m)
            await screenshot(testInfo, await terminal.row(/^\s+graph\s+register, inspect and run graphs$/), {
              annotation: { text: "A command, and what it does" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Each command has its own help too: add \`--help\` after it. For example, \`graph\` is how you work with graphs, OpenEngine's recipes for a piece of work. Type \`engine graph --help\` and press Enter.

You'll see the things you can do with graphs.`,
          async () => {
            const { output } = await terminal.run("engine graph --help")
            expect(output).toMatch(/^usage: engine graph/m)
            await screenshot(testInfo, await terminal.rows(/^\s+spec\s/, /^\s+run\s/), {
              annotation: { text: "Things you can do with graphs" },
              clip: await terminal.usedArea(),
            })
          },
        )

        await test.step(
          "Whenever you're not sure what to type next, add `--help` to what you have so far.",
          async () => {},
        )
      })
    },
  )
})
