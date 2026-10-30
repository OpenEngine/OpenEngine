import { writeFileSync } from "node:fs"
import path from "node:path"
import type { Locator, TestInfo } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { LABEL_ROOM, type Terminal } from "../fixtures/terminal.ts"
import { graphsCategory } from "./docCategories"

test.use({ terminalSession: { cols: 200, rows: 40 } })

/** The two-step graph from the last guide, so the reader has nothing new to save. */
const GRAPH = `apiVersion: openengine.cc/v1
kind: Graph
name: describe-project
plan:
  read:
    agent: claude
    prompt: Read README.md and reply with one sentence saying what this project is. You may use ls to list files and cat to read them. Don't change files.
implementation:
  suggest:
    agent: claude
    prompt: Reply with one sentence suggesting one small improvement to README.md. Don't run commands or change files.
`

/** What each step's scripted agent is asked, matched by its prompt. */
const READ = "what this project is"
const SUGGEST = "one small improvement"

/** What the reader tells the agent, and so what its next turn is asked. */
const NUDGE = "Keep the project's name. Suggest a clearer first sentence for README.md instead."

type Run = { runId: string; status: string; terminal?: boolean; pendingApprovals?: { approvalId: string }[] }
type Steering = { steeringId: string; status: string; message: string }
type Node = { node: string; executionId: string; attempt: number; status: string; runner: string; steering?: Steering[] }
type Agent = { name: string; kind: string; builtin?: boolean }

/** Ask the backend directly rather than through the CLI. */
async function get<T>(engine: Engine, route: string): Promise<T> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1${route}`, { headers })
  expect(response.ok, `GET ${route}`).toBe(true)
  return (await response.json()) as T
}

const runs = async (engine: Engine) => (await get<{ runs: Run[] }>(engine, "/runs")).runs
const run = (engine: Engine, runId: string) => get<Run>(engine, `/runs/${encodeURIComponent(runId)}`)
const nodes = async (engine: Engine, runId: string) =>
  (await get<{ nodes: Node[] }>(engine, `/runs/${encodeURIComponent(runId)}/nodes`)).nodes
const node = (engine: Engine, executionId: string) => get<Node>(engine, `/nodes/${encodeURIComponent(executionId)}`)

/** The run this test started: there is only ever one. */
async function onlyRun(engine: Engine): Promise<Run> {
  let found: Run[] = []
  await expect.poll(async () => (found = await runs(engine)).length, { message: "waiting for the run to start" }).toBe(1)
  return found[0]!
}

/** The execution of `name` in this run, once it has started. */
async function execution(engine: Engine, runId: string, name: string): Promise<Node> {
  let found: Node | undefined
  await expect.poll(async () => (found = (await nodes(engine, runId)).find((each) => each.node === name)),
    { timeout: 30_000, message: `waiting for ${name} to start` }).toBeDefined()
  return found!
}

/** A step the page shows as a paragraph of its own (see another-computer.spec.ts). */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

/**
 * Screenshot the screen down to its last row in use once it has stopped changing, with `highlight` labelled.
 * With `through`, stop at that row instead: what follows it differs from one computer to the next.
 */
async function show(filename: string, terminal: Terminal, engine: Engine, testInfo: TestInfo, highlight: Locator, label: string,
  { through }: { through?: Locator } = {}) {
  await terminal.waitForQuiet()
  await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
  // The page is the same on every run: no path on this computer may show.
  await expect(terminal.screen()).not.toContainText(path.basename(engine.root))
  await expect(terminal.screen()).not.toContainText("/engine-workspaces/")
  const used = await terminal.usedArea()
  const last = through && (await through.boundingBox())!
  const area = last ? { ...used, height: last.y + last.height - used.y } : used
  // Anchor the label beneath all output, independently of the highlighted rows.
  const anchor = terminal.page.locator("#guide-label-anchor")
  await terminal.page.evaluate(({ x, y, width, height, room, cover }) => {
    const element = document.createElement("div")
    element.id = "guide-label-anchor"
    Object.assign(element.style, {
      position: "fixed", left: `${x}px`, top: `${y + height}px`,
      width: `${width}px`, height: "1px", pointerEvents: "none",
    })
    document.body.append(element)
    if (!cover) return
    // Blank out the rows below `through`, where the label goes.
    const blank = document.createElement("div")
    blank.id = "guide-label-room"
    Object.assign(blank.style, {
      position: "fixed", left: `${x}px`, top: `${y + height}px`, width: `${width}px`, height: `${room}px`,
      background: getComputedStyle(document.body).backgroundColor, pointerEvents: "none",
    })
    document.body.append(blank)
  }, { ...area, room: LABEL_ROOM, cover: Boolean(through) })
  try {
    await screenshot(testInfo, [
      { target: highlight },
      { target: anchor, options: { annotation: {
        text: label, position: "below", showArrow: false,
        highlightStrokeStyle: "transparent", highlightFillStyle: "transparent",
      } } },
    ], { annotation: { filename }, clip: { ...area, height: area.height + LABEL_ROOM } })
  } finally {
    await terminal.page.evaluate(() => {
      for (const id of ["guide-label-anchor", "guide-label-room"]) document.getElementById(id)?.remove()
    })
  }
}

const clear = (terminal: Terminal) => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

test.describe(graphsCategory, () => {
  test.describe(
    withDocMeta("Check On Your Agents and Steer Them", {
      description: "See what each agent in a run is doing, set one straight when it's heading the wrong way without starting over, and see which agents you can use.",
      sidebar_position: 4,
      slug: "/guides/graphs/check-on-your-agents-and-steer-them",
    }),
    () => {
      test("Check on a run and steer an agent", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent heads the wrong way, and keeps working, on cue")
        test.setTimeout(120_000)
        let runId = ""
        let suggest: Node | undefined

        await test.step("[nodoc] Save the graph and say how its agents behave", async () => {
          engine.script({
            scenarios: [
              // Listed first: the turn that carries the reader's message quotes it.
              { when: NUDGE, steps: [{ type: "say", text: "Suggesting a clearer first sentence, and keeping the name." }] },
              { when: READ, steps: [
                { type: "run", command: "cat README.md" },
                { type: "say", text: "This is an example project; its README is one heading." },
              ] },
              {
                when: SUGGEST,
                steps: [
                  { type: "say", text: "I'll suggest a new name for the project." },
                  // A read action publishes the preceding narration in the live view.
                  { type: "run", command: "cat README.md", approval: false },
                  // Busy, heading the wrong way, until the reader steers it.
                  { type: "wait", seconds: 600 },
                ],
              },
            ],
          })
          writeFileSync(path.join(engine.repository, "describe-project.yaml"), GRAPH)
          await terminal.run("oe graph add describe-project.yaml")
          await terminal.run("oe repo untrust")
          await terminal.run("clear")
        })

        await step(
          `The \`oe\` command is what you type in a terminal to use OpenEngine. OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you. The way you lay out the work is a **graph**: a recipe for a piece of work, saying its steps, which agent does each one, and in what order. Each time a graph is carried out, from its first step to its last, is called a **run**.

While a run goes on, you may want to know which step it's on and how each agent is getting along. And sometimes an agent sets off in the wrong direction: it misread what you wanted, or you've thought of something it should know. Rather than stopping the run and starting it all over again, you can send that agent a message while it works, and it changes course from where it is. This guide uses the \`describe-project\` graph from [Run Your Graph and Watch It Work](./test2doc-run-your-graph-and-watch-it-work.mdx).`,
        )

        await step(
          `Start the graph without the live screen. Type \`oe graph run describe-project --no-watch\` and press Enter.

Your terminal comes straight back, and the run goes on in OpenEngine's background program. You'll see its run ID, starting with \`run-\`: copy it, because the next commands need it. You can also find it later in the **RUN ID** column of \`oe runs list\`.`,
          async () => {
            const { output } = await terminal.run("oe graph run describe-project --no-watch")
            runId = (await onlyRun(engine)).runId
            expect(output).toContain(runId)
            await show("steer-an-agent__terminal--started.png", terminal, engine, testInfo, await terminal.row(new RegExp(runId)), "Your run has started; this is its ID")
          },
        )

        await clear(terminal)

        await step(
          `Open the live screen so you can answer an agent's questions and read what it is doing. Type \`oe run watch\` followed by your run ID, and press Enter.

If you declined repository trust in the previous guide, the read step may be waiting for permission to read README.md. The run waits until you answer; waiting longer won't move it along.`,
          async () => {
            await terminal.type(`oe run watch ${runId}`)
            await terminal.press("Enter")
            await terminal.waitForText(/^Allow\? \[y\/n\][ \t]*$/m)
            expect((await run(engine, runId)).pendingApprovals).toHaveLength(1)
            await show("steer-an-agent__terminal--approval.png", terminal, engine, testInfo, await terminal.row(/^Allow\? \[y\/n\][ \t]*$/),
              "The agent is waiting for permission to read the file")
          },
        )

        await step(
          `Read the question. Here the agent asks to run \`cat README.md\`, which reads the file describing your project. Type \`y\` and press Enter to allow it.

Answer any further permission questions the same way if you want to allow the action. Then watch the lines after ┃: they show the agent's words and actions. Here the suggest agent says it will suggest a new project name. That's the direction we'll correct. Your agent's words may differ, and a short step may finish before you can steer it.`,
          async () => {
            await terminal.type("y")
            await terminal.press("Enter")
            await terminal.waitForText(/┃.*I'll suggest a new name for the project\./)
            expect((await run(engine, runId)).pendingApprovals ?? []).toHaveLength(0)
            await show("steer-an-agent__terminal--stages.png", terminal, engine, testInfo,
              await terminal.row(/┃.*I'll suggest a new name for the project\./),
              "The agent is planning a rename; you want clearer wording")
          },
        )

        await step(
          `To return to your terminal while the agent keeps working, press Ctrl-C. The screen asks what you want to do.`,
          async () => {
            await terminal.press("Control+C")
            const question = /\[d\]etach and leave it running, \[c\]ancel the run, or keep \[w\]atching\?/
            await terminal.waitForText(question)
            await show("steer-an-agent__terminal--question.png", terminal, engine, testInfo, await terminal.row(question),
              "Choose whether to leave the run going")
          },
        )

        await step(
          `Type \`d\` and press Enter to detach: close this screen and leave the run going. You can now check individual steps and send your message.`,
          async () => {
            await terminal.type("d")
            await terminal.press("Enter")
            expect(await terminal.waitForPrompt()).toBe(0)
            expect((await run(engine, runId)).status).toBe("running")
            await show("steer-an-agent__terminal--detached.png", terminal, engine, testInfo, await terminal.row(/^Detached\./),
              "The live screen is closed; the agent is still working")
          },
        )

        await clear(terminal)

        await step(
          `To see which steps are running or finished, type \`oe nodes --run\` followed by your run ID, and press Enter.

You'll see one row for each step that has started so far. **NODE** is the step's name, **STATUS** says how it's going (*running* while its agent works, *completed* when it's done), and **RUNNER** is the agent doing it. **EXECUTION** is that step's own ID: copy the one for \`suggest\`, the step that's still running. You can leave the other columns alone.

Here \`read\` is done and \`suggest\` is working. If you see only \`read\`, the run hasn't reached \`suggest\` yet; wait a moment and run the command again.`,
          async () => {
            suggest = await execution(engine, runId, "suggest")
            await expect.poll(async () => (await node(engine, suggest!.executionId)).status).toBe("running")
            const read = await execution(engine, runId, "read")
            expect(read.status).toBe("completed")
            const { output } = await terminal.run(`oe nodes --run ${runId}`)
            const row = new RegExp(`^suggest\\s+${suggest.executionId}\\s+\\d+\\s+running\\s+claude\\b`, "m")
            expect(output).toMatch(row)
            expect(output).toMatch(new RegExp(`^read\\s+${read.executionId}\\s+\\d+\\s+completed\\s+claude\\b`, "m"))
            await show("steer-an-agent__terminal--executions.png", terminal, engine, testInfo, await terminal.rows(/^NODE\s+EXECUTION\s/, new RegExp(row.source)),
              "read is done; suggest is still working, and this is its ID")
          },
        )

        await clear(terminal)

        await step(
          `To look at just that one step, type \`oe node get\` followed by the ID you copied, and press Enter.

You'll see the same row on its own. It's a quick way to check on one agent while it works.`,
          async () => {
            const { output } = await terminal.run(`oe node get ${suggest!.executionId}`)
            const row = new RegExp(`^suggest\\s+${suggest!.executionId}\\s+\\d+\\s+running\\s+claude\\b`, "m")
            expect(output).toMatch(row)
            await show("steer-an-agent__terminal--running.png", terminal, engine, testInfo, await terminal.row(new RegExp(row.source)), "suggest is still running")
          },
        )

        await clear(terminal)

        await step(
          `You saw the \`suggest\` agent propose a new name for your project, and you only wanted its README's wording improved. Tell it so. Type \`oe node steer\`, the ID you copied, and your message in quotes, then press Enter:

\`\`\`sh
oe node steer EXECUTION "${NUDGE}"
\`\`\`

Put your own step's ID where it says \`EXECUTION\`. OpenEngine takes your message and passes it to the agent as soon as it can. The reply starts with your message's own ID, then *accepted*: OpenEngine has it, and the agent will get it shortly.

If you see *only a running attempt can be steered*, the step is no longer running. Check its **STATUS** with \`oe node get\`; you can only redirect an agent while it is working.`,
          async () => {
            const { output, exitCode } = await terminal.run(`oe node steer ${suggest!.executionId} "${NUDGE}"`)
            expect(exitCode).toBe(0)
            const sent = (await node(engine, suggest!.executionId)).steering ?? []
            expect(sent).toHaveLength(1)
            expect(sent[0]!.message).toBe(NUDGE)
            expect(output).toMatch(new RegExp(`^${sent[0]!.steeringId} accepted$`, "m"))
            await show("steer-an-agent__terminal--message-accepted.png", terminal, engine, testInfo, await terminal.row(new RegExp(`^${sent[0]!.steeringId} accepted$`)),
              "OpenEngine has your message for the agent")
          },
        )

        await clear(terminal)

        await step(
          `To see whether the agent took your message on board, type \`oe node get\` and the step's ID again, and press Enter.

Below the step's row there's now a list of the messages you've sent it. **STATUS** goes from *accepted* to *delivered* when the agent has your message, then to *applied* once the agent has finished acting on it. The agent carries on from where it was: the steps already done stay done.

If a message says *undelivered*, it never reached the agent: for example, the step finished before receiving it. If it says *rejected*, OpenEngine no longer has a live connection to that agent for this step. Check the step’s **STATUS** before deciding what to do next; these messages did not change the agent’s course.`,
          async () => {
            await expect.poll(async () => (await node(engine, suggest!.executionId)).steering?.[0]?.status,
              { timeout: 30_000, message: "waiting for the agent to act on the message" }).toBe("applied")
            const { output } = await terminal.run(`oe node get ${suggest!.executionId}`)
            const { steeringId } = (await node(engine, suggest!.executionId)).steering![0]!
            expect((await node(engine, suggest!.executionId)).steering![0]!.message).toBe(NUDGE)
            const row = new RegExp(`^1\\s+${steeringId}\\s+applied\\s`, "m")
            expect(output).toMatch(row)
            const messageRow = await terminal.row(new RegExp(row.source))
            expect(await terminal.textAt(messageRow)).toContain(NUDGE)
            await show("steer-an-agent__terminal--message-applied.png", terminal, engine, testInfo, messageRow, "The agent acted on your message")
          },
        )

        await clear(terminal)

        await step(
          `Type \`oe runs list\` and press Enter.

There's still just the one run, and once \`suggest\` is done it says *completed*. You changed the agent's course without starting the work over.`,
          async () => {
            await expect.poll(async () => (await run(engine, runId)).status, { timeout: 60_000 }).toBe("completed")
            // Steered, not restarted: the same run, and the same execution of suggest.
            expect(await runs(engine)).toHaveLength(1)
            const after = (await nodes(engine, runId)).filter((each) => each.node === "suggest")
            expect(after.map((each) => each.executionId)).toEqual([suggest!.executionId])
            const { output } = await terminal.run("oe runs list")
            const row = new RegExp(`^${runId}\\s.*\\sdescribe-project v1\\s+completed\\s`)
            expect(output).toMatch(new RegExp(row.source, "m"))
            await show("steer-an-agent__terminal--completed.png", terminal, engine, testInfo, await terminal.row(row), "The same run, finished")
          },
        )
      })

      test("See which agents you can use", async ({ engine, terminal }, testInfo) => {
        await step(
          `A graph names the agent that does each step, such as \`agent: claude\`. To see which agents you can name, type \`oe agents\` and press Enter.

You'll see one row for each agent. **NAME** is what a graph calls it, and **KIND** is the AI coding assistant behind it. The ones marked *built in* are there from the start: \`claude\` is Claude Code, \`codex\` is Codex, and \`opencode\` is OpenCode. The **RUNNER** column of \`oe nodes\` shows these same names. To use one, its assistant must be installed and signed in on the computer the run happens on.`,
          async () => {
            const agents = (await get<{ agents: Agent[] }>(engine, "/agents")).agents
            expect(agents.map((agent) => agent.name)).toEqual(expect.arrayContaining(["claude", "codex", "opencode"]))
            const { output, exitCode } = await terminal.run("oe agents")
            expect(exitCode).toBe(0)
            for (const name of ["claude", "codex", "opencode"]) {
              expect(output).toMatch(new RegExp(`^${name}\\s+${name}\\s.*built in$`, "m"))
            }
            // The details below the table name where each assistant is installed on this computer.
            const table = await terminal.rows(/^NAME\s+KIND\s/, /^opencode\s+opencode\s/)
            await show("steer-an-agent__terminal--agents.png", terminal, engine, testInfo, table, "The agents your graphs can use", { through: table })
          },
        )

        await step(
          "Next, to learn everything else a graph file can say, such as more steps or one agent checking another's work, see [Graphs](../../graphs.md).",
        )
      })
    },
  )
})
