import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect } from "../fixtures/terminal.ts"
import { gettingStartedCategory } from "./docCategories"

test.use({ terminalSession: { cols: 100, rows: 46 } })

test.describe(gettingStartedCategory, () => {
  test.describe(
    withDocMeta("Getting Help", {
      description: "How to see everything the oe command can do, and get help with any part of it.",
      sidebar_position: 2,
      slug: "/guides/getting-started/getting-help",
    }),
    () => {
      test("See what you can do", async ({ terminal }, testInfo) => {
        await test.step(
          `Every OpenEngine command starts with \`oe\`. To see all of them, type \`oe --help\` and press Enter.

You'll see a list of commands, each with a short note on what it does.`,
          async () => {
            const { output } = await terminal.run("oe --help")
            expect(output).toMatch(/^usage: oe/m)
            await screenshot(testInfo, await terminal.row(/^\s+graph\s+register, inspect and run graphs$/), {
              annotation: { text: "A command, and what it does", position: "right" },
            })
          },
        )

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          `Each command has its own help too: add \`--help\` after it. For example, \`graph\` is how you work with graphs, OpenEngine's recipes for a piece of work. Type \`oe graph --help\` and press Enter.

You'll see the things you can do with graphs.`,
          async () => {
            const { output } = await terminal.run("oe graph --help")
            expect(output).toMatch(/^usage: oe graph/m)
            await screenshot(testInfo, await terminal.rows(/^\s+spec\s/, /^\s+list\s/), {
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
