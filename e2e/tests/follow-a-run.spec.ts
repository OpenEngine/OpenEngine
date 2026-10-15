import { existsSync, writeFileSync } from "node:fs"
import path from "node:path"
import type { Locator, TestInfo } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type AgentScript, type Engine } from "../fixtures/engine.ts"
import type { Terminal } from "../fixtures/terminal.ts"
import { graphsCategory } from "./docCategories"

test.use({ terminalSession: { cols: 120, rows: 40 }, requireCommandApproval: true })

/** A graph with a step in each of two stages, so the screen has something to move through. */
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

/** What each step's scripted agent says, matched by its prompt. */
const READ = "what this project is"
const SUGGEST = "one small improvement"
const suggest = { when: SUGGEST, steps: [{ type: "say" as const, text: "Add a sentence under the heading saying what the project does." }] }

/** The rows of the live view a guide points at. Marks are any status: ✓, ○, ?, ■ or a spinner frame. */
const STAGES = /^\s+\S Plan ─── \S Implement ─── \S Review$/
const graphRow = (read: string, suggest: string) => new RegExp(`^\\s+Graph\\s+✓ workspace → ${read} read → ${suggest} suggest$`)
const QUESTION = /^read asks to .*ls/
const ALLOW = /Allow\? \[y\/n\]/
const INTERRUPTED = /\[d\]etach and leave it running, \[c\]ancel the run, or keep \[w\]atching\?/

type Run = { runId: string; status: string; terminal?: boolean; workspace?: { path: string }; pendingApprovals?: { approvalId: string }[] }

/** What the backend has for its runs, asked of it directly rather than through the CLI. */
async function runs(engine: Engine): Promise<Run[]> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  const response = await fetch(`${engine.url}/api/v1/runs`, { headers })
  expect(response.ok).toBe(true)
  return (await response.json()).runs as Run[]
}

async function run(engine: Engine, runId: string): Promise<Run> {
  const headers = { Authorization: `Bearer ${engine.env.ENGINE_SERVICE_TOKEN}` }
  // The run list can appear before the runtime has written its first snapshot.
  let response: Response
  await expect.poll(async () => {
    response = await fetch(`${engine.url}/api/v1/runs/${encodeURIComponent(runId)}`, { headers })
    return response.status
  }).not.toBe(404)
  expect(response!.ok).toBe(true)
  return (await response!.json()) as Run
}

/** The run this test started: there is only ever one. */
async function onlyRun(engine: Engine): Promise<Run> {
  let found: Run[] = []
  await expect.poll(async () => (found = await runs(engine)).length, { message: "waiting for the run to start" }).toBe(1)
  return found[0]!
}

/** A step the page shows as a paragraph of its own (see another-computer.spec.ts). */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

/** Screenshot the screen down to its last row in use once it has stopped changing, with `highlight` labelled. */
async function show(terminal: Terminal, engine: Engine, testInfo: TestInfo, highlight: Locator, label: string) {
  await terminal.waitForQuiet()
  await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
  // The page is the same on every run: no path on this computer, such as where a run's work is, may show.
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

/**
 * Answer the question about trusting this folder with no, so agents ask
 * before running a command, as the guide tells the reader to. Answered ahead
 * of the run, because the question names the folder, a path on this computer
 * that the page must not show.
 */
async function distrust(terminal: Terminal) {
  await test.step("[nodoc] Say agents must ask before running commands here", async () => {
    await terminal.run("engine repo untrust")
    await terminal.run("clear")
  })
}

/** Save the graph, as the reader did in the last guide, and say how its agents will behave. */
async function prepare(engine: Engine, terminal: Terminal, script: AgentScript) {
  await test.step("[nodoc] Save the graph", async () => {
    engine.script(script)
    const file = path.join(engine.repository, "describe-project.yaml")
    writeFileSync(file, GRAPH)
    await terminal.run("engine graph add describe-project.yaml")
  })
  await distrust(terminal)
}

/** Start the run and wait for the live view. */
async function start(terminal: Terminal) {
  await terminal.type("engine graph run describe-project")
  await terminal.press("Enter")
  await terminal.waitForText(/^\s+Graph\s+/m)
}

test.describe(graphsCategory, () => {
  test.describe(
    withDocMeta("Run Your Graph and Watch It Work", {
      description: "Start a graph, watch its agents work through it step by step, answer when an agent asks you something, and step away or stop it.",
      sidebar_position: 2,
      slug: "/guides/graphs/run-your-graph-and-watch-it-work",
    }),
    () => {
      test("Start your graph and watch it", async ({ engine, terminal }, testInfo) => {
        test.setTimeout(engine.agentMode === "real" ? 300_000 : 90_000)
        engine.script({
          scenarios: [
            {
              when: READ,
              steps: [
                { type: "say", text: "Reading the README to see what this project is." },
                // Long enough to show the run part way through, short enough not to keep anyone waiting.
                { type: "run", command: "cat README.md" },
                { type: "wait", seconds: 4 },
                { type: "say", text: "This is an example project; its README is one heading." },
              ],
            },
            suggest,
          ],
        })

        await step(
          `The \`engine\` command is what you type in a terminal to use OpenEngine. OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you. The way you lay out the work is a **graph**: a recipe for a piece of work, saying its steps, which agent does each one, and in what order.

In [Save Your First Graph](./save-your-first-graph) you saved a graph. Now you'll use it. Each time a graph is carried out, from its first step to its last, is called a **run**. While a run goes on, your terminal shows which step it's on and what the agent is saying, so you can see the work happen instead of waiting in the dark. You can also answer an agent that stops to ask you something, walk away and come back, or stop a run you no longer want.`,
        )

        await step(
          `This guide uses a graph with two steps. In your project's folder, save this as \`describe-project.yaml\`, then type \`engine graph add describe-project.yaml\` and press Enter to save it to OpenEngine, as in the last guide:

\`\`\`yaml
${GRAPH}\`\`\`

\`plan\` and \`implementation\` are two of a graph's **stages**: groups of steps that happen one after another. The first stage plans the work and the second does it. Here each has one step: \`read\` looks at your README, then \`suggest\` proposes an improvement to it.`,
          async () => {
            writeFileSync(path.join(engine.repository, "describe-project.yaml"), GRAPH)
            await terminal.run("engine graph add describe-project.yaml")
            await show(terminal, engine, testInfo, await terminal.row(/^registered describe-project v1\s/), "Your graph is saved")
          },
        )

        await distrust(terminal)

        await step(
          `Start the graph. Type \`engine graph run describe-project\` and press Enter.

The first time you run a graph in a folder, OpenEngine asks whether to trust it. Trusting a folder lets agents change files and run commands there without asking you first. Type \`n\` and press Enter: the agents will check with you before they run a command. You can change that choice later.`,
          async () => {
            await terminal.type("engine graph run describe-project")
            await terminal.press("Enter")
          },
        )

        const answered = new Set<string>()
        const answerQuestions = async (state: Run) => {
          for (const approval of state.pendingApprovals ?? []) {
            if (answered.has(approval.approvalId)) continue
            await terminal.waitForText(ALLOW)
            await show(terminal, engine, testInfo, await terminal.row(ALLOW), "Read the question before allowing the command")
            await terminal.type("y")
            await terminal.press("Enter")
            answered.add(approval.approvalId)
          }
        }

        await step(
          "If an agent asks to run a command, read the question. Type y and press Enter to allow it. Here the agent reads README.md, the file that describes your project.",
          async () => {
            const active = await onlyRun(engine)
            if (engine.agentMode === "stub") await terminal.waitForText(ALLOW)
            await answerQuestions(await run(engine, active.runId))
          },
        )

        await step(
          `Now watch. The screen redraws itself as the run goes on:

- The top line is the graph's name and version, the run ID, how it's going, and how long it has taken.
- The next line is the stages. A spinning mark is the stage in progress, ✓ is one that's done, and ○ is one still to come. – means this graph has nothing in that stage.
- *Graph* lists every step in order, with the same marks, and *Now* says which step is running and which agent is doing it.
- At the bottom, after ┃, is what the agent is saying and doing right now.

If you see *cannot reach backend*, OpenEngine's background program isn't running. A **backend** is a computer where that program runs: this one, or a remote machine. Follow the [instructions to start OpenEngine](../../cli-reference.md#engine-daemon-start), then start the graph again.`,
          async () => {
            await terminal.waitForText(/^\s+Graph\s+/m)
            if (engine.agentMode === "stub") await terminal.waitForText(/┃ ⚙ cat README\.md/)
            const running = await onlyRun(engine)
            await answerQuestions(await run(engine, running.runId))
            if (engine.agentMode === "real") return // Real agents can finish or ask again at any point.
            await expect.poll(async () => (await run(engine, running.runId)).status).toBe("running")
            await show(terminal, engine, testInfo, await terminal.rows(STAGES, /^\s+Graph\s+/), "The plan stage is in progress; its step, read, is running")
            // The screen settled part way through the run, not only once it had ended.
            expect((await run(engine, running.runId)).status).toBe("running")
            await expect(terminal.screen()).toContainText(" · running · ")
          },
        )

        await step(
          `Keep watching until the run ends. When the last step is done, every mark turns to ✓, the top line says *completed*, and below it you'll see what the last step's agent replied.

If an agent asks to run a command, read the question. To allow it, type \`y\` and press Enter. Answer any further questions the same way when you want to allow the requested action. Once the work is done, the run ends on its own.`,
          async () => {
            const active = await onlyRun(engine)
            await expect.poll(async () => {
              const state = await run(engine, active.runId)
              await answerQuestions(state)
              return state.terminal
            }, { timeout: engine.agentMode === "real" ? 280_000 : 60_000,
                 message: "answering command approvals until the run ends" }).toBe(true)
            if (engine.agentMode === "stub") expect(answered.size).toBe(1)
            const exitCode = await terminal.waitForPrompt({ timeout: 60_000 })
            expect(exitCode).toBe(0)
            const { runId } = await onlyRun(engine)
            expect((await run(engine, runId)).status).toBe("completed")
            await show(terminal, engine, testInfo, await terminal.rows(STAGES, graphRow("✓", "✓")), "Every stage and step is done")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

        await step(
          `To see your runs afterwards, type \`engine runs list\` and press Enter.

You'll see one row for each run, newest first, with the graph it ran and its status: *completed* when it finished, *cancelled* if you stopped it, and *running* if it's still going. You can leave the other columns alone.`,
          async () => {
            const { output } = await terminal.run("engine runs list")
            expect(output).toMatch(/^run-\S+\s.*\sdescribe-project v1\s+completed\s/m)
            await show(terminal, engine, testInfo, await terminal.row(/^run-\S+\s.*\sdescribe-project v1\s+completed\s/), "Your run, and that it finished")
          },
        )
      })

      test("Answer an agent's question", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent asks a question on cue")
        await prepare(engine, terminal, {
          scenarios: [
            {
              when: READ,
              steps: [
                { type: "say", text: "I'll list the files first." },
                { type: "run", command: "ls" },
                { type: "say", text: "This is an example project; its README is one heading." },
              ],
            },
            suggest,
          ],
        })

        await step(
          `Sometimes an agent stops and asks you something, such as whether it may run a command. The run waits for your answer, and the question appears right in the screen you're watching.

Start the graph again with \`engine graph run describe-project\`, and answer \`n\` if you're asked whether to trust the folder.`,
          async () => start(terminal),
        )

        await step(
          `When an agent asks, the screen stops redrawing and shows the question: which step is asking, and what it wants to do. Here the \`read\` step asks to run \`ls\`, a command that lists the files in a folder. The question mark in the *Graph* line shows the step waiting for you.

Type \`y\` and press Enter to let it, or \`n\` to say no. If you say no, the command is not run and this run ends as failed. Start the graph again if you want to try again.`,
          async () => {
            await terminal.waitForText(ALLOW)
            const { runId } = await onlyRun(engine)
            expect((await run(engine, runId)).pendingApprovals ?? []).toHaveLength(1)
            await show(terminal, engine, testInfo, await terminal.rows(QUESTION, ALLOW), "An agent is asking to run a command")
            await terminal.type("y")
            await terminal.press("Enter")
          },
        )

        await step(
          `The run picks up where it was and goes on to the end, as before.`,
          async () => {
            expect(await terminal.waitForPrompt({ timeout: 60_000 })).toBe(0)
            const { runId } = await onlyRun(engine)
            const ended = await run(engine, runId)
            expect(ended.status).toBe("completed")
            expect(ended.pendingApprovals ?? []).toHaveLength(0)
            await show(terminal, engine, testInfo, await terminal.rows(STAGES, graphRow("✓", "✓")), "Your answer let the run finish")
          },
        )
      })

      test("Step away and come back", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent waits on cue")
        await prepare(engine, terminal, {
          scenarios: [
            {
              when: READ,
              steps: [
                { type: "say", text: "I'll list the files first." },
                { type: "run", command: "ls" },
                { type: "say", text: "This is an example project; its README is one heading." },
              ],
            },
            suggest,
          ],
        })
        let runId = ""

        await step(
          `A run doesn't need your terminal open. It goes on in OpenEngine's background program, so you can close the screen that shows it, do something else, and come back to it later, for example when an agent has a question for you.

Start the graph with \`engine graph run describe-project\`, and answer \`n\` if you're asked whether to trust the folder.`,
          async () => {
            await start(terminal)
            await terminal.waitForText(ALLOW)
            runId = (await onlyRun(engine)).runId
            await show(terminal, engine, testInfo, await terminal.row(/^\s+Graph\s+/), "Your graph is running")
          },
        )

        await step(
          `To step away, press Ctrl-C: hold down the Control key and press C.

The screen stops and asks what you want: *d* to detach, which closes this screen and leaves the run going; *c* to cancel the run; or *w* to keep watching.`,
          async () => {
            await terminal.press("Control+C")
            await terminal.waitForText(INTERRUPTED)
            await show(terminal, engine, testInfo, await terminal.row(INTERRUPTED), "Choose what happens to the run")
          },
        )

        await step(
          `Type \`d\` and press Enter.

The screen says *Detached* and gives you the command to come back with, which includes the run ID. \`local\` means the backend on this computer; it's there from the start. Copy it somewhere, or find the ID later in the **RUN ID** column of \`engine runs list\`. The run is still going, waiting for the answer to its question.`,
          async () => {
            await terminal.type("d")
            await terminal.press("Enter")
            expect(await terminal.waitForPrompt()).toBe(0)
            const detached = new RegExp(`^Detached\\. Reattach with: engine run watch ${runId} --backend local$`)
            await terminal.waitForText(new RegExp(detached.source, "m"))
            const waiting = await run(engine, runId)
            expect(["running", "awaiting_approval"]).toContain(waiting.status)
            expect(waiting.pendingApprovals ?? []).toHaveLength(1)
            await show(terminal, engine, testInfo, await terminal.row(detached), "The run goes on; this is how to come back to it")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

        await step(
          `To come back, type the command it gave you, \`engine run watch\` followed by the run ID, and press Enter.

The same screen comes back, showing where the run is now. Since the agent is still waiting, its question is asked again.`,
          async () => {
            await terminal.type(`engine run watch ${runId}`)
            await terminal.press("Enter")
            // Asked again, below the command that came back to it.
            await expect.poll(async () => (await terminal.text()).split("$ engine run watch").at(-1)).toMatch(ALLOW)
            expect((await run(engine, runId)).pendingApprovals ?? []).toHaveLength(1)
            await show(terminal, engine, testInfo, await terminal.rows(STAGES, ALLOW), "You're back, and the question is waiting")
          },
        )

        await step(
          `Type \`y\` and press Enter, and the run goes on to the end.`,
          async () => {
            await terminal.type("y")
            await terminal.press("Enter")
            expect(await terminal.waitForPrompt({ timeout: 60_000 })).toBe(0)
            expect((await run(engine, runId)).status).toBe("completed")
            await show(terminal, engine, testInfo, await terminal.rows(STAGES, graphRow("✓", "✓")), "Your run finished after you came back")
          },
        )
      })

      test("Stop a run", async ({ engine, terminal }, testInfo) => {
        test.skip(engine.agentMode !== "stub", "only the scripted agent is still working on cue")
        await prepare(engine, terminal, {
          scenarios: [
            {
              when: READ,
              steps: [
                { type: "say", text: "Reading the README to see what this project is." },
                // Busy until the run is cancelled.
                { type: "run", command: "cat README.md" },
                { type: "wait", seconds: 600 },
              ],
            },
            suggest,
          ],
        })
        let runId = ""

        await step(
          `If you started the wrong graph, or no longer need what it's doing, you can stop it. Start the graph with \`engine graph run describe-project\`, and answer \`n\` if you're asked whether to trust the folder.`,
          async () => {
            await start(terminal)
            await terminal.waitForText(ALLOW)
            await show(terminal, engine, testInfo, await terminal.row(ALLOW), "The agent asks before reading the file")
          },
        )

        await step(
          "Read the question. Type y and press Enter to let the agent read README.md before you stop its work.",
          async () => {
            await terminal.type("y")
            await terminal.press("Enter")
            await terminal.waitForText(/┃ ⚙ cat README\.md/)
            runId = (await onlyRun(engine)).runId
            await show(terminal, engine, testInfo, await terminal.row(/^\s+Graph\s+/), "Your graph is running")
          },
        )

        await step(
          `While it's running, press Ctrl-C. When the screen asks what you want, type \`c\` and press Enter.

OpenEngine stops the agent and ends the run. Below the last view of its progress, a summary shows the run ID and *cancelled*.`,
          async () => {
            await terminal.press("Control+C")
            await terminal.waitForText(INTERRUPTED)
            await terminal.type("c")
            await terminal.press("Enter")
            await terminal.waitForPrompt({ timeout: 60_000 })
            await expect.poll(async () => (await run(engine, runId)).status, { timeout: 30_000 }).toBe("cancelled")
            await terminal.waitForText(new RegExp(`^${runId} · cancelled$`, "m"))
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(`^${runId} · cancelled$`)), "The run is stopped")
          },
        )

        await test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

        await step(
          `Type \`engine runs list\` and press Enter. Your run is there with the status *cancelled*. A cancelled run can't be picked up again; to do the work after all, start the graph again.`,
          async () => {
            const { output } = await terminal.run("engine runs list")
            const row = new RegExp(`^${runId}\\s.*\\sdescribe-project v1\\s+cancelled\\s`, "m")
            expect(output).toMatch(row)
            await show(terminal, engine, testInfo, await terminal.row(new RegExp(row.source)), "The run you stopped")
          },
        )

        await step(
          "Next, to learn everything else a graph file can say, such as more steps or one agent checking another's work, see [Graphs](../../graphs.md).",
        )
      })
    },
  )
})

// Exercise interactive controls with a real agent as well as the scripted provider.
for (const action of ["approve", "decline", "reconnect", "cancel"] as const) {
  test(`Interactive run: ${action} @skip-docs`, async ({ engine, terminal }) => {
    test.setTimeout(300_000)
    const graph = GRAPH.replace(
      "Read README.md and reply with one sentence saying what this project is. You may use ls to list files and cat to read them. Don't change files.",
      "Use the Bash tool to run exactly touch approval-check.txt before replying with one sentence saying what this project is. This creates an empty file in this disposable example project. Do not use other tools or commands.",
    )
    engine.script({ scenarios: [
      { when: READ, steps: [{ type: "run", command: "touch approval-check.txt" }, { type: "say", text: "This is an example project." }] },
      suggest,
    ] })
    writeFileSync(path.join(engine.repository, "describe-project.yaml"), graph)
    await terminal.run("engine graph add describe-project.yaml")
    await distrust(terminal)
    await start(terminal)
    await terminal.waitForText(ALLOW, { timeout: 240_000 })
    const { runId } = await onlyRun(engine)
    expect((await run(engine, runId)).pendingApprovals).toHaveLength(1)
    if (action === "reconnect" || action === "cancel") {
      await terminal.press("Control+C")
      await terminal.waitForText(INTERRUPTED)
      await terminal.type(action === "cancel" ? "c" : "d")
      await terminal.press("Enter")
      const code = await terminal.waitForPrompt()
      if (action === "cancel") {
        expect(code).toBe(1)
        expect((await run(engine, runId)).status).toBe("cancelled")
        return
      }
      expect(code).toBe(0)
      expect((await run(engine, runId)).pendingApprovals).toHaveLength(1)
      await terminal.run("clear")
      await terminal.type(`engine run watch ${runId}`)
      await terminal.press("Enter")
      await terminal.waitForText(ALLOW)
    }
    await terminal.type(action === "decline" ? "n" : "y")
    await terminal.press("Enter")
    expect(await terminal.waitForPrompt({ timeout: 240_000 })).toBe(action === "decline" ? 1 : 0)
    expect((await run(engine, runId)).status).toBe(action === "decline" ? "failed" : "completed")
    const ended = await run(engine, runId)
    expect(ended.pendingApprovals ?? []).toHaveLength(0)
    expect(ended.workspace?.path).toBeTruthy()
    expect(existsSync(path.join(ended.workspace!.path, "approval-check.txt"))).toBe(action !== "decline")
  })
}
