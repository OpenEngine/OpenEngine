import { mkdirSync } from "node:fs"
import { defineConfig } from "@playwright/test"
import { projects, expectConfig, sharedUse, webServerConfig } from "./playwright.config.ts"
import "@test2doc/playwright/types"

// The site's guides directory; generated, so it is gitignored there. test2doc
// cleans it before writing and gives up silently when it does not exist yet.
const outputDir = "../site/docs/guides"
mkdirSync(outputDir, { recursive: true })

export default defineConfig({
  testDir: "./tests",
  globalTeardown: "./support/tunnel-cleanup.ts",
  expect: expectConfig,
  use: {
    ...sharedUse,
    test2doc: {
      annotationDefaults: {
        showArrow: true,
        highlightFillStyle: "rgba(0, 0, 0, 0)",
        highlightStrokeStyle: "rgba(255, 0, 0, 0.8)",
        labelBoxFillStyle: "rgba(0, 0, 0, 0.8)",
        labelBoxStrokeStyle: "rgba(255, 0, 0, 1)",
        fillStyle: "rgba(255, 255, 255, 1)",
      },
    },
  },
  reporter: [["@test2doc/playwright", { outputDir }]],
  // One worker so pages and screenshots come out in a stable order.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  projects,
  webServer: webServerConfig,
})
