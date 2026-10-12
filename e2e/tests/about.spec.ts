import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { test, expect } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { engineBin } from "../support/engine"
import { gettingStartedCategory } from "./docCategories"

const run = promisify(execFile)

test.describe(gettingStartedCategory, () => {
  test.describe(
    withDocMeta("About These Guides", {
      description: "How these guides are made and what you need to follow them.",
      sidebar_position: 1,
      slug: "/guides/getting-started/about-these-guides",
    }),
    () => {
      test("How to follow these guides", async () => {
        await test.step("[nodoc] The engine CLI these guides drive starts", async () => {
          // Playwright sets FORCE_COLOR in its workers, which colours argparse's help.
          const { FORCE_COLOR: _, ...env } = process.env
          const { stdout } = await run(engineBin, ["--help"], { env: { ...env, NO_COLOR: "1" } })
          expect(stdout).toContain("usage: engine")
        })

        await test.step(
          "Every guide here is recorded from a real terminal session running the `engine` CLI, so the commands and output you see are what you will get.",
          async () => {},
        )

        await test.step(
          "Install the CLI first. Each guide then starts from a fresh daemon on your machine, so you can follow along step by step.",
          async () => {},
        )
      })
    },
  )
})
