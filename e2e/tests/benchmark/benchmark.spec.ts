import { execFile } from "node:child_process"
import { randomBytes } from "node:crypto"
import { mkdirSync } from "node:fs"
import path from "node:path"
import { promisify } from "node:util"
import { expect } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { startEngine, type Engine } from "../../fixtures/engine.ts"
import { Terminal, test } from "../../fixtures/terminal.ts"
import { BENCH_REPO, BENCH_TOPIC, benchDirectory, getRepo, gh, recordProcess, whoami } from "../../support/benchmark.ts"
import { issues } from "../../fixtures/calculator/issues.ts"
import { milestone } from "../../fixtures/calculator/milestone.ts"
import { freePort } from "../../support/freePort.ts"
import { show, step } from "../../support/show.ts"
import { benchmarkCategory } from "../docCategories"

// The end-to-end benchmark (docs/plans/e2e-benchmark.md). It runs only through `npm run oe-benchmark`,
// never `npm test` or `npm run doc`, because of its tag. The "setup" project in
// playwright-benchmark.config.ts has already cleared the last run's leftovers by the time it starts.
// Each issue adds its steps to the journey below, in order.

const run = promisify(execFile)

/** The webhook's shared secret: new each run, handed to OpenEngine and the terminal as an environment variable. */
const WEBHOOK_SECRET = randomBytes(24).toString("hex")

/** What GitHub tells OpenEngine about: the events docs/github-webhooks.md says to subscribe to. */
const EVENTS = ["issue_comment", "pull_request_review_comment", "issues", "pull_request", "check_suite", "check_run", "status"]

test.use({
  terminalSession: {
    cols: 140,
    rows: 40,
    env: {
      ENGINE_GITHUB_WEBHOOK_SECRET: WEBHOOK_SECRET,
      // Vim as it comes, not as this computer's ~/.vimrc has it, so the screenshot is the same everywhere.
      VIMINIT: "set nocompatible number ttimeout ttimeoutlen=50",
      // The terminal acts as the same GitHub account as the harness, wherever it keeps its login.
      ...(process.env.GH_CONFIG_DIR ? { GH_CONFIG_DIR: process.env.GH_CONFIG_DIR } : {}),
    },
  },
})

test.describe(benchmarkCategory, { tag: ["@benchmark"] }, () => {
  test.describe(
    withDocMeta("Build a Calculator With OpenEngine", {
      description: "How to have OpenEngine build a whole project from a list of GitHub issues, step by step.",
      sidebar_position: 1,
      slug: "/start-to-finish/build-a-calculator",
    }),
    () => {
      test("The journey", async ({ terminal, context }, testInfo) => {
        const owner = await whoami()
        // The calculator repository, as `gh` names it, and as GitHub's API addresses it.
        const repo = `${owner}/${BENCH_REPO}`
        const api = `repos/${repo}`

        await step("[nodoc] The terminal is signed in to GitHub as the same account as the harness", async () => {
          // They can differ: a `gh` wrapper may pick its account by directory, and the terminal starts somewhere
          // else. A repository or webhook made as the wrong account is one the harness's teardown can't see.
          const { output } = await terminal.run("gh api user --jq .login")
          const seen = output.trim().split("\n").at(-1)!.trim()
          expect(
            seen,
            `The terminal's gh is signed in as ${seen} but the harness's is ${owner}. Run the benchmark with GH_CONFIG_DIR set ` +
              `to the config directory of the account to use, so that both agree: GH_CONFIG_DIR=~/.config/gh-bench npm run oe-benchmark`,
          ).toBe(owner)
          // Nothing of this check on screen in the screenshots that follow.
          await terminal.run("clear")
        })

        // OpenEngine's port is chosen first: the tunnel needs it, and OpenEngine needs the tunnel's address.
        const port = freePort()

        const tunnelUrl = await step(
          "OpenEngine runs on your computer, and GitHub can't reach your computer directly. A Cloudflare quick tunnel gives it a temporary public address. Open a second terminal for it, because the tunnel keeps running, and start the tunnel there. It needs no Cloudflare account.",
          async () => {
            // An empty home, like a fresh computer: cloudflared reads ~/.cloudflared/config.yml, and the config of a
            // named tunnel there sends a quick tunnel's traffic to a 404.
            const home = path.join(benchDirectory(), "tunnel-home")
            mkdirSync(home, { recursive: true })
            // Tall enough that the address stays in view while the tunnel's log fills the screen.
            const tunnel = await Terminal.open(await context.newPage(), { cols: 140, rows: 40, env: { HOME: home } })
            const command = `cloudflared tunnel --url http://127.0.0.1:${port}`
            await tunnel.type(command)
            await tunnel.press("Enter")
            await tunnel.waitForText(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/, { timeout: 90_000 })
            const url = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/.exec(await tunnel.text())![0]
            // Shown now, while the address is still on screen: the tunnel's log scrolls it away.
            await show(tunnel, testInfo, await tunnel.row(new RegExp(url.replace(/\./g, "\\."))), "Your temporary public address")
            // The address prints before the tunnel is connected: wait for the connection too.
            await tunnel.waitForText(/Registered tunnel connection/, { timeout: 90_000 })
            // The tunnel is the terminal's foreground program, so find it by its command line, and record it so
            // that teardown stops it however the run ends.
            const { stdout } = await run("pgrep", ["-f", command])
            await recordProcess(Number(stdout.trim().split("\n")[0]))
            return url
          },
        )

        // Until the real setup step exists, OpenEngine is the e2e daemon: the same program with a scripted agent.
        const engine = await step(
          "OpenEngine only answers requests for addresses it knows, so tell it the tunnel's. Open OpenEngine's configuration file, `engine.toml`, in a text editor and add a line at the top: `public_url`, set to the tunnel's address. Then save the file and start OpenEngine. It reads the webhook secret from an environment variable, never from the file.",
          async () => {
            return startEngine({
              port,
              env: { ENGINE_GITHUB_WEBHOOK_SECRET: WEBHOOK_SECRET },
              // OpenEngine starts from the file this edit leaves, so the guide shows the real edit.
              beforeStart: async ({ root }) => {
                // Where the file is on this computer is its business, not the guide's: keep the path off screen.
                await terminal.run(`cd '${root}'`)
                await terminal.run("clear")
                await terminal.type("vim engine.toml")
                await terminal.press("Enter")
                await terminal.waitForText(/"engine\.toml"/)
                // `gg` goes to the top of the file; `O` opens a new line above it, in insert mode.
                await terminal.type("ggO")
                await terminal.type(`public_url = "${tunnelUrl}"`)
                await terminal.press("Escape")
                await show(
                  terminal,
                  testInfo,
                  await terminal.row(/public_url = "https/),
                  "Add the tunnel's address as public_url. The file is usually at ~/.config/openengine/engine.toml",
                )
                await terminal.type(":wq")
                await terminal.press("Enter")
                await terminal.waitForPrompt()
              },
            })
          },
        )

        try {
          await step(
            "Your project lives in a GitHub repository. Create one with the `gh` command, GitHub's own tool for the terminal. It is created under the account `gh` is signed in as.",
            async () => {
              await terminal.run(`gh repo create ${BENCH_REPO} --public --add-readme --description "A calculator, built by OpenEngine."`)
              await show(terminal, testInfo, await terminal.row(/github\.com\/[^/\s]+\/oe-bench/), "Your new repository")
            },
          )

          await step(
            "Give it a topic, a label GitHub shows on the repository. OpenEngine's benchmark uses it to recognise this repository as its own.",
            async () => {
              await terminal.run(`gh repo edit ${repo} --add-topic ${BENCH_TOPIC}`)
              expect((await getRepo(owner, BENCH_REPO))?.topics, "GitHub lists the topic").toContain(BENCH_TOPIC)
            },
          )

          await step(
            "OpenEngine works from GitHub issues, so fill the repository with the plan: a milestone called Calculator, and one issue for each piece of the calculator. Each issue says which issue it depends on, in its text and as a link GitHub shows as blocked by, so OpenEngine takes them in order.",
            async () => {
              const number = (await gh("api", `${api}/milestones`, "-f", `title=${milestone.title}`, "-f", `description=${milestone.description}`, "--jq", ".number")).trim()
              // Created in order, each depending on the one before it, so the previous issue is already known.
              const numbers: string[] = []
              let previousId = ""
              for (const issue of issues) {
                const body = [
                  ...issue.description,
                  "## Acceptance criteria",
                  issue.criteria.map((criterion) => `- [ ] ${criterion}`).join("\n"),
                  ...(numbers.length ? [`Depends on #${numbers.at(-1)}`] : []),
                ].join("\n\n")
                const created = JSON.parse(await gh("api", `${api}/issues`, "-f", `title=${issue.title}`, "-f", `body=${body}`, "-F", `milestone=${number}`)) as { number: number; id: number }
                // The text above is what OpenEngine reads; the link is what GitHub shows, as "Blocked by #N" on the issue.
                if (previousId) await gh("api", "-X", "POST", `${api}/issues/${created.number}/dependencies/blocked_by`, "-F", `issue_id=${previousId}`)
                numbers.push(String(created.number))
                previousId = String(created.id)
              }
              await terminal.run(`gh issue list --repo ${repo} --milestone ${milestone.title} --state open --limit 30`)
              await show(terminal, testInfo, await terminal.row(/Scaffold the calculator page/), "The calculator's issues")
              // GitHub's listing can trail the issues just created, so wait for it to show them all.
              const listIssues = async () => JSON.parse(await gh("api", `${api}/issues?milestone=${number}&state=open&per_page=100`)) as { number: number; body: string }[]
              await expect.poll(async () => (await listIssues()).length, { timeout: 30_000, message: "every issue is in the milestone" }).toBe(issues.length)
              expect(await gh("api", `${api}/issues/${numbers.at(-1)}/dependencies/blocked_by`, "--jq", "[.[].number]"), "GitHub shows the last issue as blocked by the one before it").toContain(`${numbers.at(-2)}`)
              expect((await listIssues()).find((issue) => issue.number === Number(numbers.at(-1)))?.body, "the last issue links to the one before it").toContain(
                `Depends on #${numbers.at(-2)}`,
              )
            },
          )

          const hook = await step(
            "Now point the repository at that address. This is a webhook: GitHub sends a message to a web address whenever something happens in the repository, such as a comment or a merged pull request. The messages are signed with a shared secret, so OpenEngine can tell GitHub sent them.",
            async () => {
              // The secret reaches the command as an environment variable, so it never appears on screen.
              const events = EVENTS.map((event) => `-f 'events[]=${event}'`).join(" ")
              const { output } = await terminal.run(
                `gh api ${api}/hooks -f name=web -F active=true -f 'config[url]=${tunnelUrl}/api/github/events' ` +
                  `-f 'config[content_type]=json' -f 'config[secret]='"$ENGINE_GITHUB_WEBHOOK_SECRET" ${events} --jq .id`,
              )
              const id = output.trim().split("\n").at(-1)!.trim()
              expect(id, "GitHub answers with the new webhook's number").toMatch(/^\d+$/)
              await show(terminal, testInfo, await terminal.row(new RegExp(`^${id}$`)), "The webhook's number")
              return id
            },
          )

          const hookPath = `${api}/hooks/${hook}`
          await step(
            "Check that it works. Ask GitHub to send a test message. GitHub keeps a log of every message it sends and the answer it got, so you can see whether OpenEngine accepted it.",
            async () => {
              await terminal.run(`gh api -X POST ${hookPath}/pings`)
              // GitHub's own log of what OpenEngine answered: 200 means it passed the signature check.
              await expect
                .poll(async () => (JSON.parse(await gh("api", `${hookPath}/deliveries`)) as { status_code: number }[]).map((d) => d.status_code), {
                  timeout: 60_000,
                  message: "waiting for GitHub to record OpenEngine's answer",
                })
                .toContain(200)
              await terminal.run(`gh api ${hookPath}/deliveries --jq '.[] | "\\(.event) \\(.status_code)"'`)
              await show(terminal, testInfo, await terminal.row(/ping 200/), "OpenEngine accepted the message")
            },
          )

          await step(
            "A message that isn't signed with the secret is refused, so nobody else can send OpenEngine work. Send one yourself to see it: the answer is 401, which means \"not allowed\".",
            async () => {
              const { output } = await terminal.run(`curl -s -o /dev/null -w '%{http_code}\\n' -X POST ${tunnelUrl}/api/github/events`)
              expect(output.trim().split("\n").at(-1)).toBe("401")
              await show(terminal, testInfo, await terminal.row(/^401$/), "OpenEngine refuses it")
            },
          )
        } finally {
          await engine.stop()
        }
      })
    },
  )
})
