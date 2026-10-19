import { writeFileSync } from "node:fs"
import path from "node:path"
import { test, expect, type Engine } from "../fixtures/engine.ts"

// A summary is what an agent wrote, so it can contain the widest and most
// unbroken line Markdown allows. The run page must not be stretched past its
// container by one: a long line wraps inside the card, and a wide code block
// scrolls within its box rather than growing the page.

const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: summarize
plan:
  report:
    agent: claude
    prompt: Reply with one sentence summarizing README.md. Don't run commands or change files.
`

const UNBROKEN = "a".repeat(500)
const VIEWPORT = { width: 640, height: 800 }

async function runs(engine: Engine): Promise<{ runId: string; status: string }[]> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1/runs`, { headers })
  expect(response.ok).toBe(true)
  return (await response.json()).runs
}

test("A long summary line and a wide code block stay inside the step card @skip-docs", async ({ engine, terminal, browser }) => {
  test.skip(engine.agentMode !== "stub", "only the scripted agent writes the injected summary on cue")
  test.setTimeout(90_000)
  engine.script({ scenarios: [{ when: "summarizing README.md", steps: [{
    type: "say",
    text: `${UNBROKEN}\n\n\`\`\`\n${UNBROKEN}\n\`\`\``,
  }] }] })
  writeFileSync(path.join(engine.repository, "summarize.yaml"), GRAPH)
  await terminal.run("engine graph add summarize.yaml")
  await terminal.run("engine repo trust")
  await terminal.type("engine graph run summarize")
  await terminal.press("Enter")
  expect(await terminal.waitForPrompt({ timeout: 60_000 })).toBe(0)
  const [run] = await runs(engine)
  expect(run?.status).toBe("completed")

  const context = await browser.newContext({
    baseURL: engine.url,
    viewport: VIEWPORT,
    extraHTTPHeaders: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` },
  })
  const page = await context.newPage()
  try {
    await page.goto(`/runs/${encodeURIComponent(run!.runId)}`)
    const summary = page.locator(".step-summary")
    await expect(summary).toContainText(UNBROKEN)
    // The step's group arrives collapsed; open it to reach the summary on screen.
    await page.locator("details.step-group > summary").click()
    // The page itself does not grow past the viewport.
    expect(await page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(VIEWPORT.width)
    // The wide code block scrolls inside its box instead of stretching the card.
    const pre = summary.locator("pre")
    await expect(pre).toBeVisible()
    const metrics = await pre.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }))
    expect(metrics.scrollWidth, "the code block is wide enough to need a scrollbar")
      .toBeGreaterThan(metrics.clientWidth)
  } finally {
    await context.close()
  }
})
