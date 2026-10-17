import { writeFileSync } from "node:fs"
import path from "node:path"
import type { Locator, TestInfo } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { injectMarkdown } from "@test2doc/playwright/injectMarkdown"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type AgentScript, type Engine } from "../fixtures/engine.ts"
import type { Terminal } from "../fixtures/terminal.ts"
import { graphsCategory } from "./docCategories"

test.use({ terminalSession: { cols: 120, rows: 40 } })

/** A one-step graph that says how often to run and what to do each time. */
const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: check-readme
loop:
  every: 1h
  instruction: Look at README.md and suggest one small improvement.
implementation:
  suggest:
    agent: claude
    prompt: Reply with one sentence suggesting one small improvement to README.md. Don't run commands or change files.
`

/** What the scripted agent is asked, matched by its prompt. */
const SUGGEST = "one small improvement"

/** Busy until something stops it, so a run is still going when the guide looks at the loop. */
const BUSY: AgentScript = {
  scenarios: [{
    when: SUGGEST,
    steps: [
      { type: "say", text: "Reading the README to find something to improve." },
      { type: "wait", seconds: 600 },
    ],
  }],
}

/** The command that sets the loop up, with both limits. */
const ADD = "engine loop add check-readme --every 1h --max-spend 5 --max-prs 1"

type Loop = {
  loopId: string
  name: string
  state: string
  pauseReason: string | null
  everySeconds: number
  nextRunAt: string | null
  activeRunId: string | null
  limits: { maxPrs: number | null; maxSpendUsd: number | null }
}
type Run = { runId: string; status: string; terminal?: boolean }

/** Ask the backend directly rather than through the CLI. */
async function get<T>(engine: Engine, route: string): Promise<T> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1${route}`, { headers })
  expect(response.ok, `GET ${route}`).toBe(true)
  return (await response.json()) as T
}

const loops = async (engine: Engine) => (await get<{ loops: Loop[] }>(engine, "/loops")).loops
const loop = (engine: Engine, name: string) => get<Loop>(engine, `/loops/${encodeURIComponent(name)}`)
const run = (engine: Engine, runId: string) => get<Run>(engine, `/runs/${encodeURIComponent(runId)}`)

/** The loop's run, once its first one has started. Loops check what's due every few seconds. */
async function activeRun(engine: Engine): Promise<string> {
  let runId: string | null = null
  await expect.poll(async () => (runId = (await loop(engine, "check-readme")).activeRunId),
    { timeout: 60_000, message: "waiting for the loop's first run to start" }).not.toBeNull()
  return runId!
}

/** A step the page shows as a paragraph of its own (see another-computer.spec.ts). */
function step(text: string, body: () => Promise<void>) {
  return test.step(`\n${text}\n`, body)
}

/** Screenshot the screen down to its last row in use once it has stopped changing, with `highlight` labelled. */
async function show(terminal: Terminal, engine: Engine, testInfo: TestInfo, highlight: Locator, label: string) {
  await terminal.waitForQuiet()
  await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
  // The page is the same on every run: no path on this computer may show.
  await expect(terminal.screen()).not.toContainText(path.basename(engine.root))
  await expect(terminal.screen()).not.toContainText("/engine-workspaces/")
  const area = await terminal.usedArea()
  // Anchor the label beneath all output, independently of the highlighted rows.
  const anchor = terminal.page.locator("#guide-label-anchor")
  await terminal.page.evaluate(({ x, y, width, height }) => {
    const element = document.createElement("div")
    element.id = "guide-label-anchor"
    Object.assign(element.style, {
      position: "fixed", left: `${x}px`, top: `${y + height}px`,
      width: `${width}px`, height: "1px", pointerEvents: "none",
    })
    document.body.append(element)
  }, area)
  try {
    await screenshot(testInfo, [
      { target: highlight },
      { target: anchor, options: { annotation: {
        text: label, position: "below", showArrow: false,
        highlightStrokeStyle: "transparent", highlightFillStyle: "transparent",
      } } },
    ], { clip: await terminal.usedArea({ labelBelow: true }) })
  } finally {
    await anchor.evaluate((element) => element.remove())
  }
}

const clear = (terminal: Terminal) => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

/**
 * Answer the question about trusting this folder ahead of time, with no, so
 * agents ask before running a command. The question names the folder, a path
 * on this computer that the page must not show.
 */
async function distrust(terminal: Terminal) {
  await test.step("[nodoc] Say agents must ask before running commands here", async () => {
    await terminal.run("engine repo untrust")
    await terminal.run("clear")
  })
}

/** Save the graph and set up its loop, as the first part of the guide did. */
async function prepare(engine: Engine, terminal: Terminal, script: AgentScript) {
  await test.step("[nodoc] Save the graph and set up its loop", async () => {
    engine.script(script)
    writeFileSync(path.join(engine.repository, "check-readme.yaml"), GRAPH)
    await terminal.run("engine graph add check-readme.yaml")
    await terminal.run("engine repo untrust")
    await terminal.run(ADD)
    await terminal.run("clear")
  })
}

test.describe(graphsCategory, () => {
  test.describe(
    withDocMeta("Run Your Graph on a Schedule", {
      description: "Have a graph run again and again on its own, with limits on how much it may spend and how many pull requests it may open, and pause or stop it when you need to.",
      sidebar_position: 3,
      slug: "/guides/graphs/run-your-graph-on-a-schedule",
    }),
    () => {
      test("Set up a loop and check on it", async ({ engine, terminal }, testInfo) => {
        test.setTimeout(engine.agentMode === "real" ? 300_000 : 120_000)
        engine.script(BUSY)

        await test.step("Read about scheduled graphs", async () => {
          await injectMarkdown(testInfo, `The \`engine\` command is what you type in a terminal to use OpenEngine. OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you. The way you lay out the work is a **graph**: a recipe for a piece of work, saying its steps, which agent does each one, and in what order. Each time a graph is carried out, from its first step to its last, is called a **run**.

Some work is never finished for good: keeping a README up to date, fixing one flaky test at a time, tidying a little more each day. Instead of starting the same graph yourself every few hours, you can set up a **loop**: a graph set to run again and again on a schedule, with limits on how much it may spend. You choose how often it runs, how much money its agents may spend in all, and how many pull requests (proposed changes to your code) it may open. When it reaches a limit, it pauses itself, and you can pause or stop it yourself at any time.`)
          await injectMarkdown(testInfo, `To read everything a loop can do, type \`engine loop spec\` and press Enter.

You'll see detailed reference material about loops: how to set one up, how often it can run, its limits, and how to pause it. You don't need any of it to follow this guide, but it's there whenever you want the details.`)
          const { output } = await terminal.run("engine loop spec")
          expect(output).toContain("engine loop add GRAPH")
          expect(output).toContain("--max-prs")
          expect(output).toContain("--max-spend")
          // The spec says how to stop a loop for good, not only how to pause it.
          expect(output).toMatch(/`engine loop stop NAME_OR_ID`/)
          // Show the beginning of the reference, including the command the reader typed.
          await terminal.page.evaluate(() => window.__term!.scrollToTop())
          await show(terminal, engine, testInfo, await terminal.row(/engine loop spec/), "The detailed reference for scheduled graphs")
        })

        await clear(terminal)

        await step(
          `A loop needs a graph to run. This guide uses one with a single step. In your project's folder, save this as \`check-readme.yaml\`, then type \`engine graph add check-readme.yaml\` and press Enter to save it to OpenEngine, as in [Save Your First Graph](./save-your-first-graph):

\`\`\`yaml
${GRAPH}\`\`\`

The \`loop\` part is new. \`every: 1h\` says to run the graph once an hour, and \`instruction\` is what to ask for on each run. You can use \`30m\` for half an hour or \`1d\` for once a day; a loop can't run more often than once a minute.`,
          async () => {
            writeFileSync(path.join(engine.repository, "check-readme.yaml"), GRAPH)
            await terminal.run("engine graph add check-readme.yaml")
            await show(terminal, engine, testInfo, await terminal.row(/^registered check-readme v1\s/), "Your graph is saved")
          },
        )

        await distrust(terminal)

        await step(
          `Set up the loop. Type \`${ADD}\` and press Enter.

\`--max-spend 5\` sets a $5 limit on reported agent usage across every run. OpenEngine stops the active run when reported spending reaches the limit; usage it cannot price and other costs, such as hosting, are not included. \`--max-prs 1\` means it may open at most one pull request. When the loop reaches either limit, it pauses itself. \`--every 1h\` says how often to run; you can leave it out when the graph says, as this one does.

OpenEngine shows the loop's name, that it's *active*, how often it runs and when it runs next, and what it has used so far of each limit. Its first run starts right away. If OpenEngine asks whether to trust the folder, answer \`n\`, as in the last guide.`,
          async () => {
            const { output } = await terminal.run(ADD)
            expect(output).toMatch(/^check-readme \(loop-\S+\)\s+active$/m)
            const added = await loop(engine, "check-readme")
            expect(added.state).toBe("active")
            expect(added.everySeconds).toBe(3600)
            expect(added.limits).toMatchObject({ maxPrs: 1, maxSpendUsd: 5 })
            await show(terminal, engine, testInfo, await terminal.row(/^pull requests 0\/1, spend \$0\.00\/\$5\.00/), "No pull requests and no money spent yet, with the limits you set")
          },
        )

        await clear(terminal)

        await step(
          `To see all your loops, type \`engine loops list\` and press Enter.

You'll see one row for each loop, with its graph, its state (*active*, *paused* or *stopped*), how often it runs, when it runs next, and how far it is toward each limit: *PRS* is pull requests opened out of the most allowed, and *SPEND* is money spent out of the most allowed.`,
          async () => {
            const { output } = await terminal.run("engine loops list")
            const row = /^check-readme\s+check-readme v1\s+active\s+1h\s/m
            expect(output).toMatch(row)
            expect((await loops(engine)).map((each) => each.name)).toEqual(["check-readme"])
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(row.source)), "Your loop is active and runs every hour")
          },
        )

        await clear(terminal)

        await step(
          `To look at one loop more closely, type \`engine loop get check-readme\` and press Enter.

Along with what the list shows, you'll see the run it's doing now, if there is one, under *active run*. Once a run has finished, *latest output* shows what its last step replied. To follow the run as it goes, type \`engine run watch\` and the run ID, as in [Run Your Graph and Watch It Work](./run-your-graph-and-watch-it-work).`,
          async () => {
            const runId = await activeRun(engine)
            const { output } = await terminal.run("engine loop get check-readme")
            expect(output).toMatch(/^check-readme \(loop-\S+\)\s+active$/m)
            if (engine.agentMode === "real") return // A real agent may already be done.
            expect((await run(engine, runId)).status).toBe("running")
            expect(output).toContain(`active run ${runId}`)
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(`^active run ${runId}$`)), "The loop's first run is going now")
          },
        )
      })

      test("Pause a loop and start it again", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent is still working on cue")
        test.setTimeout(120_000)
        await prepare(engine, terminal, BUSY)
        const runId = await activeRun(engine)

        await step(
          `If you want a loop to take a break, for example while you review what it has done so far, pause it. A paused loop starts no new runs until you start it again. A run that's already going carries on to its end.

Type \`engine loop pause check-readme --reason "Reviewing its work"\` and press Enter. The reason is for you and anyone else who looks at the loop later; you can leave it out.`,
          async () => {
            const { output } = await terminal.run(`engine loop pause check-readme --reason "Reviewing its work"`)
            expect(output).toMatch(/^check-readme \(loop-\S+\)\s+paused: Reviewing its work$/m)
            const paused = await loop(engine, "check-readme")
            expect(paused.state).toBe("paused")
            expect(paused.nextRunAt).toBeNull()
            // Pausing leaves the run that's going alone.
            expect((await run(engine, runId)).status).toBe("running")
            await show(terminal, engine, testInfo, await terminal.row(/^check-readme \(loop-\S+\)\s+paused: Reviewing its work$/), "The loop is paused, and why")
          },
        )

        await clear(terminal)

        await step(
          `Type \`engine loops list\` and press Enter. Your loop shows *paused*, with your reason under *PAUSED BECAUSE*. When a loop pauses itself because it reached a limit, this is where it says which one.`,
          async () => {
            const { output } = await terminal.run("engine loops list")
            const row = /^check-readme\s+check-readme v1\s+paused\s+1h\s+-\s.*Reviewing its work\s*$/m
            expect(output).toMatch(row)
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(row.source)), "Paused, with the reason you gave")
          },
        )

        await clear(terminal)

        await step(
          `To start it again, type \`engine loop resume check-readme\` and press Enter. The loop is *active* again and goes back to its schedule.

If it paused itself because it reached a limit, it won't start again until you raise that limit: add a higher one, such as \`--max-spend 10\` or \`--max-prs 2\`, to the same command.`,
          async () => {
            const { output } = await terminal.run("engine loop resume check-readme")
            expect(output).toMatch(/^check-readme \(loop-\S+\)\s+active$/m)
            expect((await loop(engine, "check-readme")).state).toBe("active")
            await show(terminal, engine, testInfo, await terminal.row(/^check-readme \(loop-\S+\)\s+active$/), "The loop is running on its schedule again")
          },
        )
      })

      test("Stop a loop for good", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent is still working on cue")
        test.setTimeout(120_000)
        await prepare(engine, terminal, BUSY)
        const runId = await activeRun(engine)

        await step(
          `When you no longer want a loop at all, stop it. Unlike pausing, stopping is for good: the loop starts no more runs, the run it's doing now is stopped too, and it can't be started again. To have the graph run on a schedule again later, set up a new loop with a different name using \`--name\`.

Type \`engine loop stop check-readme\` and press Enter. OpenEngine shows the loop as *stopped*.`,
          async () => {
            const { output } = await terminal.run("engine loop stop check-readme")
            expect(output).toMatch(/^check-readme \(loop-\S+\)\s+stopped$/m)
            const stopped = await loop(engine, "check-readme")
            expect(stopped.state).toBe("stopped")
            expect(stopped.nextRunAt).toBeNull()
            await expect.poll(async () => (await run(engine, runId)).status, { timeout: 30_000 }).toBe("cancelled")
            await expect.poll(async () => (await loop(engine, "check-readme")).activeRunId).toBeNull()
            await show(terminal, engine, testInfo, await terminal.row(/^check-readme \(loop-\S+\)\s+stopped$/), "The loop is stopped for good")
          },
        )

        await test.step("[nodoc] A stopped loop can't be started again", async () => {
          await terminal.run("engine loop resume check-readme", { exitCode: 1 })
          expect((await loop(engine, "check-readme")).state).toBe("stopped")
          await terminal.run("clear")
        })

        await step(
          `Type \`engine loops list\` and press Enter. Your loop is still listed, as *stopped*, so you can look back at what it did. Its last run shows as *cancelled* in \`engine runs list\`.

Next, to learn everything else a graph file can say, such as more steps or one agent checking another's work, see [Graphs](../../graphs.md).`,
          async () => {
            const { output } = await terminal.run("engine loops list")
            const row = /^check-readme\s+check-readme v1\s+stopped\s+1h\s+-\s+-\s/m
            expect(output).toMatch(row)
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(row.source)), "Your loop is stopped and will start no more runs")
          },
        )
      })
    },
  )
})
