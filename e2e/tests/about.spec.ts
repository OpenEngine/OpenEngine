import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { test, expect } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { engineBin } from "../support/engine"
import { gettingStartedCategory } from "./docCategories"

const run = promisify(execFile)

test.describe(gettingStartedCategory, () => {
  test.describe(
    withDocMeta("Before You Start", {
      description: "What OpenEngine is, what these guides cover, and what you need to follow them.",
      sidebar_position: 1,
      slug: "/guides/getting-started/before-you-start",
    }),
    () => {
      test("What you need", async () => {
        await test.step("[nodoc] The engine command these guides use starts", async () => {
          // Playwright sets FORCE_COLOR in its workers, which colours argparse's help.
          const { FORCE_COLOR: _, ...env } = process.env
          const { stdout } = await run(engineBin, ["--help"], { env: { ...env, NO_COLOR: "1" } })
          expect(stdout).toContain("usage: oe")
        })

        await test.step(
          "OpenEngine has AI coding agents, such as Claude Code, work on your code for you, step by step, in the way you lay out. You use it by typing `oe` commands in a terminal: the window on your computer where you type commands.",
          async () => {},
        )

        await test.step(
          "To follow these guides, you need OpenEngine installed. Open a terminal, type `oe --help` and press Enter. If you see a list of commands, you're ready. If you see *command not found*, install OpenEngine first, following the [Overview](/docs/).",
          async () => {},
        )

        await test.step(
          "Each guide was made by running the real commands and capturing what came back, so what you see on these pages is what you'll get. Start with **Getting Help**, next.",
          async () => {},
        )
      })
    },
  )
})
