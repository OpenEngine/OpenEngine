import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect } from "../fixtures/terminal.ts"
import { gettingStartedCategory } from "./docCategories"

test.use({ terminalSession: { cols: 100, rows: 38 } })

test.describe(gettingStartedCategory, () => {
  test.describe(
    withDocMeta("Getting Help", {
      description: "How to see every engine command, and the options of any one of them.",
      sidebar_position: 2,
      slug: "/guides/getting-started/getting-help",
    }),
    () => {
      test("How to get help with the engine CLI", async ({ terminal }, testInfo) => {
        await test.step("Run `engine --help` to see every command and what it does.", async () => {
          const { output } = await terminal.run("engine --help")
          expect(output).toMatch(/^usage: engine/m)
          await screenshot(testInfo, await terminal.row(/^\s+graph\s+register, inspect and run graphs$/), {
            annotation: { text: "Each command, and what it does" },
          })
        })

        await test.step("[nodoc] Clear the screen", async () => {
          await terminal.run("clear")
        })

        await test.step(
          "Add `--help` to any command to see its own subcommands and options. For example, `engine graph --help`:",
          async () => {
            const { output } = await terminal.run("engine graph --help")
            expect(output).toMatch(/^usage: engine graph/m)
            await screenshot(testInfo, await terminal.rows(/^\s+spec\s/, /^\s+run\s/), {
              annotation: { text: "What you can do with graphs" },
              clip: await terminal.usedArea(),
            })
          },
        )
      })
    },
  )
})
