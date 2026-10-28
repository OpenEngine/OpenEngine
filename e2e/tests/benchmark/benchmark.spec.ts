import { randomBytes } from "node:crypto"
import { expect } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { Terminal, test } from "../../fixtures/terminal.ts"
import { BENCH_REPO, BENCH_TOPIC, containerSession, getRepo, gh, whoami } from "../../support/benchmark.ts"
import { issues } from "../../fixtures/calculator/issues.ts"
import { milestone } from "../../fixtures/calculator/milestone.ts"
import { show, step } from "../../support/show.ts"
import { benchmarkCategory } from "../docCategories"

// The end-to-end benchmark (docs/plans/e2e-benchmark.md). It runs only through `npm run oe-benchmark`,
// never `npm test` or `npm run doc`, because of its tag. The "setup" project in
// playwright-benchmark.config.ts has already cleared the last run's leftovers by the time it starts.
// Each issue adds its steps to the journey below, in order.

/** The webhook's shared secret: new each run, handed to OpenEngine and the terminal as an environment variable. */
const WEBHOOK_SECRET = randomBytes(24).toString("hex")

/** What GitHub tells OpenEngine about: the events docs/github-webhooks.md says to subscribe to. */
const EVENTS = ["issue_comment", "pull_request_review_comment", "issues", "pull_request", "check_suite", "check_run", "status"]

// The journey happens on a clean computer: a Docker container the setup project starts, with the logins it needs copied
// in (see startContainer). Every terminal in the guide is a shell inside it, so a screenshot shows nothing of the
// developer's own setup, and what the run installs or starts is removed with the container.
test.use({
  checkoutEngine: false,
  shell: true,
  terminalSession: containerSession({ env: { ENGINE_GITHUB_WEBHOOK_SECRET: WEBHOOK_SECRET } }),
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

        await step(
          "OpenEngine is open source. Start from its latest version: clone the repository, which downloads its source code, go into it, and note the exact version you got.\n\n```sh\ngit clone --depth 1 https://github.com/OpenEngine/OpenEngine.git\ncd OpenEngine\ngit rev-parse HEAD\n```",
          async () => {
            await terminal.run("git clone --depth 1 https://github.com/OpenEngine/OpenEngine.git", { timeout: 180_000 })
            await terminal.run("cd OpenEngine")
            await terminal.run("clear")
            const { output } = await terminal.run("git rev-parse HEAD")
            const sha = output.trim().split("\n").at(-1)!.trim()
            expect(sha, "git answers with a commit").toMatch(/^[0-9a-f]{40}$/)
            // The version under test, for the report.
            testInfo.annotations.push({ type: "openengine-sha", description: sha })
            await show("benchmark__terminal--version.png", terminal, testInfo, await terminal.row(new RegExp(`^${sha}$`)), "The exact version of OpenEngine")
          },
        )

        await step(
          "Install it. The installer builds OpenEngine and puts the `oe` command in your home folder. It takes a few minutes the first time. `--no-start` leaves OpenEngine stopped until you start it below.\n\n```sh\nscripts/install-local.sh --no-start\noe --version\n```",
          async () => {
            const started = Date.now()
            await terminal.run("scripts/install-local.sh --no-start", { timeout: 900_000 })
            // Timed on its own, so the time it takes does not count against OpenEngine's run.
            testInfo.annotations.push({ type: "install-seconds", description: String(Math.round((Date.now() - started) / 1000)) })
            await terminal.run("clear")
            await terminal.run("oe --version")
            await show("benchmark__terminal--installed.png", terminal, testInfo, await terminal.row(/^\d+\.\d+\.\d+$/), "OpenEngine is installed")
          },
        )

        // OpenEngine's default port: the guide uses the defaults, as a new user would.
        const port = 4364

        const tunnelCommand = `cloudflared tunnel --url http://127.0.0.1:${port}`
        const tunnelUrl = await step(
          "OpenEngine runs on your computer, and GitHub can't reach your computer directly. A Cloudflare quick tunnel gives it a temporary public address. Open a second terminal for it, because the tunnel keeps running, and start the tunnel there. It needs no Cloudflare account.\n\n" +
            "```sh\n" + tunnelCommand + "\n```",
          async () => {
            // Tall enough that the address stays in view while the tunnel's log fills the screen.
            const tunnel = await Terminal.open(await context.newPage(), containerSession(), { checkoutEngine: false, shell: true })
            await tunnel.type(tunnelCommand)
            await tunnel.press("Enter")
            await tunnel.waitForText(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/, { timeout: 90_000 })
            const url = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/.exec(await tunnel.text())![0]
            // Shown now, while the address is still on screen: the tunnel's log scrolls it away.
            await show("benchmark__terminal--tunnel.png", tunnel, testInfo, await tunnel.row(new RegExp(url.replace(/\./g, "\\."))), "Your temporary public address")
            // The address prints before the tunnel is connected: wait for the connection too.
            await tunnel.waitForText(/Registered tunnel connection/, { timeout: 90_000 })
            return url
          },
        )

        await step(
          "OpenEngine only answers requests for addresses it knows, so tell it the tunnel's. Open OpenEngine's configuration file, `engine.toml`, in a text editor and add a line at the top: `public_url`, set to the tunnel's address.\n\n```sh\nvim ~/.config/openengine/engine.toml\n```\n\nThe line to add:\n\n```toml\npublic_url = \"https://<your tunnel address>\"\n```",
          async () => {
            await terminal.type("vim ~/.config/openengine/engine.toml")
            await terminal.press("Enter")
            await terminal.waitForText(/engine\.toml"/)
            // `gg` goes to the top of the file; `O` opens a new line above it, in insert mode.
            await terminal.type("ggO")
            await terminal.type(`public_url = "${tunnelUrl}"`)
            await terminal.press("Escape")
            await show(
              "benchmark__terminal--public-url.png",
              terminal,
              testInfo,
              await terminal.row(/public_url = "https/),
              "Add the tunnel's address as public_url. The file is at ~/.config/openengine/engine.toml",
            )
            await terminal.type(":wq")
            await terminal.press("Enter")
            await terminal.waitForPrompt()
            await terminal.run("clear")
          },
        )

        await step(
          "OpenEngine reads the webhook secret from a file named `.env`, kept next to `engine.toml` and readable only by you. Here the secret is in an environment variable named `ENGINE_GITHUB_WEBHOOK_SECRET`, so it never appears on screen.\n\n```sh\nprintf 'ENGINE_GITHUB_WEBHOOK_SECRET=%s\\n' \"$ENGINE_GITHUB_WEBHOOK_SECRET\" > ~/.config/openengine/.env\nchmod 600 ~/.config/openengine/.env\nls -la ~/.config/openengine\n```",
          async () => {
            await terminal.run(`printf 'ENGINE_GITHUB_WEBHOOK_SECRET=%s\\n' "$ENGINE_GITHUB_WEBHOOK_SECRET" > ~/.config/openengine/.env`)
            await terminal.run("chmod 600 ~/.config/openengine/.env")
            await terminal.run("ls -la ~/.config/openengine")
            await show("benchmark__terminal--secret-file.png", terminal, testInfo, await terminal.row(/-rw-------.* \.env$/), "The secret is in a file only you can read")
          },
        )

        try {
          await step(
            "Start OpenEngine. It keeps running in the background.\n\n```sh\noe daemon start\noe daemon status\n```",
            async () => {
              await terminal.run("oe daemon start", { timeout: 120_000 })
              await terminal.run("oe daemon status")
              await show("benchmark__terminal--health.png", terminal, testInfo, await terminal.row(/health: ready/), "OpenEngine is running")
            },
          )

          await step(
            "Connect OpenEngine to GitHub. It uses the `gh` command you are already signed in with, so there is nothing to paste.\n\n```sh\noe connect gh\n```",
            async () => {
              await terminal.run("oe connect gh")
              await show("benchmark__terminal--github-login.png", terminal, testInfo, await terminal.row(/GitHub CLI selected/), "OpenEngine will use your GitHub login")
            },
          )

          await step(
            "Check that OpenEngine has what it needs. `oe doctor` lists each requirement and whether it is in place, including whether the coding agent, Claude, is signed in.\n\n```sh\noe doctor\n```",
            async () => {
              const { output } = await terminal.run("oe doctor", { timeout: 120_000 })
              const login = output.split("\n").find((line) => line.includes("claude login"))
              // Found here, not minutes into a run: a run that starts without it fails at its first agent step.
              expect(login, "Claude is not signed in where OpenEngine runs. Sign in with `claude /login`, then run the benchmark again.").toMatch(/^\s*ok\s/)
              expect(output, "oe doctor found a problem").not.toMatch(/^\s*error\s/m)
              // The line names the Claude account, which has no place in a guide: it is covered, and the line before is shown.
            await show("benchmark__terminal--claude-login.png", terminal, testInfo, await terminal.row(/ok\s+claude:/), "Claude is installed and signed in; the account is hidden", {
              masked: [await terminal.row(/claude login/)],
            })
            },
          )

          await step(
            "`tdd-guide` has a second coding agent, Codex, write the code, so check that it is signed in as well.\n\n```sh\ncodex login status\n```",
            async () => {
              // Found here, like Claude's sign-in: a run that starts without it fails at the step that implements.
              await terminal.run("codex login status").catch(() => {
                throw new Error("Codex is not signed in where OpenEngine runs. Sign in with `codex login`, then run the benchmark again.")
              })
              await show("benchmark__terminal--codex-login.png", terminal, testInfo, await terminal.row(/logged in/i), "Codex is signed in")
            },
          )

          await step(
            "OpenEngine works through graphs, the steps it follows for a job. Register `tdd-guide`, the one that works an issue test first, and list what is registered.\n\n```sh\noe graph add docs/examples/graphs/tdd-guide.yaml\noe graphs\n```",
            async () => {
              await terminal.run("oe graph add docs/examples/graphs/tdd-guide.yaml")
              await terminal.run("oe graphs")
              await show("benchmark__terminal--graph-registered.png", terminal, testInfo, await terminal.row(/^tdd-guide\s/), "tdd-guide is registered")
            },
          )

          await step(
            "Your project lives in a GitHub repository. Create one with the `gh` command, GitHub's own tool for the terminal. It is created under the account `gh` is signed in as.\n\n" +
              "```sh\ngh repo create " + BENCH_REPO + " --public --add-readme --description \"A calculator, built by OpenEngine.\"\n```",
            async () => {
              await terminal.run(`gh repo create ${BENCH_REPO} --public --add-readme --description "A calculator, built by OpenEngine."`)
              await show("benchmark__terminal--repository-created.png", terminal, testInfo, await terminal.row(/github\.com\/[^/\s]+\/oe-bench/), "Your new repository")
            },
          )

          await step(
            "Give it a topic, a label GitHub shows on the repository. OpenEngine's benchmark uses it to recognise this repository as its own.\n\n" +
              "```sh\ngh repo edit <your-account>/" + BENCH_REPO + " --add-topic " + BENCH_TOPIC + "\n```",
            async () => {
              await terminal.run(`gh repo edit ${repo} --add-topic ${BENCH_TOPIC}`)
              expect((await getRepo(owner, BENCH_REPO))?.topics, "GitHub lists the topic").toContain(BENCH_TOPIC)
            },
          )

          await step(
            "Git needs two things to work on your behalf: permission to use your GitHub login, and a name and email to put on the commits OpenEngine makes.\n\n" +
              "```sh\ngh auth setup-git\ngit config --global user.name \"<your name>\"\ngit config --global user.email \"<your email>\"\n```",
            async () => {
              await terminal.run("gh auth setup-git")
              await terminal.run(`git config --global user.name "${owner}"`)
              await terminal.run(`git config --global user.email "${owner}@users.noreply.github.com"`)
              await terminal.run("clear")
              await terminal.run("git config --global --list")
              await show("benchmark__terminal--git-identity.png", terminal, testInfo, await terminal.row(/^user\.name=/), "Git knows who you are, and uses your GitHub login")
            },
          )

          await step(
            "Tell OpenEngine about the repository. `oe repo add` downloads it onto your computer and remembers where it is, so OpenEngine knows which folder to work in. It takes effect straight away.\n\n" +
              "```sh\noe repo add https://github.com/<your-account>/" + BENCH_REPO + " --path ~/" + BENCH_REPO + "\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`oe repo add https://github.com/${repo} --path ~/${BENCH_REPO}`, { timeout: 180_000 })
              await terminal.run(`ls ~/${BENCH_REPO}`)
              await show("benchmark__terminal--repository-cloned.png", terminal, testInfo, await terminal.row(/^README\.md$/), "The repository is on your computer")
            },
          )

          await step(
            "OpenEngine's deploy workflow will publish the calculator with GitHub Pages, so turn Pages on, with GitHub Actions as its source.\n\n" +
              "```sh\ngh api -X POST repos/<your-account>/" + BENCH_REPO + "/pages -f build_type=workflow --jq .build_type\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`gh api -X POST ${api}/pages -f build_type=workflow --jq .build_type`)
              expect(await gh("api", `${api}/pages`, "--jq", ".build_type"), "Pages publishes from GitHub Actions").toBe("workflow\n")
              await show("benchmark__terminal--pages-workflow.png", terminal, testInfo, await terminal.row(/^workflow$/), "Pages publishes from GitHub Actions")
            },
          )

          await step(
            "The workflows OpenEngine adds need to write to the repository, to publish releases, and to open pull requests. Allow both.\n\n" +
              "```sh\ngh api -X PUT repos/<your-account>/" + BENCH_REPO + "/actions/permissions/workflow \\\n  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true\ngh api repos/<your-account>/" + BENCH_REPO + "/actions/permissions/workflow\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`gh api -X PUT ${api}/actions/permissions/workflow -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true`)
              await terminal.run(`gh api ${api}/actions/permissions/workflow`)
              const permissions = JSON.parse(await gh("api", `${api}/actions/permissions/workflow`)) as { default_workflow_permissions: string; can_approve_pull_request_reviews: boolean }
              expect(permissions, "workflows can write, and can approve pull requests").toEqual({ default_workflow_permissions: "write", can_approve_pull_request_reviews: true })
              await show("benchmark__terminal--workflow-permissions.png", terminal, testInfo, await terminal.row(/default_workflow_permissions/), "Workflows can write and open pull requests")
            },
          )

          await step(
            "OpenEngine merges its pull requests by squashing them into one commit. Allow that, and make the commit's title the pull request's title, which is what the releases are built from.\n\n" +
              "```sh\ngh api -X PATCH repos/<your-account>/" + BENCH_REPO + " -F allow_squash_merge=true \\\n  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY \\\n  --jq '{allow_squash_merge, squash_merge_commit_title}'\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(
                `gh api -X PATCH ${api} -F allow_squash_merge=true -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY --jq '{allow_squash_merge, squash_merge_commit_title}'`,
              )
              const settings = JSON.parse(await gh("api", api)) as { allow_squash_merge: boolean; squash_merge_commit_title: string }
              expect(settings, "squash merging is allowed, titled by the pull request").toMatchObject({ allow_squash_merge: true, squash_merge_commit_title: "PR_TITLE" })
              await show("benchmark__terminal--squash-merge.png", terminal, testInfo, await terminal.row(/allow_squash_merge/), "Squash merging is on")
            },
          )

          await step(
            "OpenEngine works from GitHub issues. To give it something to build, this guide uses an example: a small calculator app, written as a milestone called Calculator with an issue for each piece of it. Each issue depends on the one before, in its text and as a link GitHub shows as blocked by, so OpenEngine takes them in order. With your own project you would use your own issues. List the example ones with:\n\n" +
              "```sh\ngh issue list --repo <your-account>/" + BENCH_REPO + " --milestone " + milestone.title + "\n```",
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
              await show("benchmark__terminal--issues.png", terminal, testInfo, await terminal.row(/Scaffold the calculator page/), "The calculator's issues")
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
            "Now point the repository at that address. This is a webhook: GitHub sends a message to a web address whenever something happens in the repository, such as a comment or a merged pull request. The messages are signed with a shared secret, so OpenEngine can tell GitHub sent them. The secret is the one from the `.env` file; here it is still in the environment variable. The command prints the webhook's number.\n\n" +
              "```sh\ngh api repos/<your-account>/" + BENCH_REPO + "/hooks -f name=web -F active=true \\\n" +
              "  -f 'config[url]=https://<your tunnel address>/api/github/events' \\\n" +
              "  -f 'config[content_type]=json' -f 'config[secret]='\"$ENGINE_GITHUB_WEBHOOK_SECRET\" \\\n" +
              "  " + EVENTS.map((event) => `-f 'events[]=${event}'`).join(" ") + " --jq .id\n```",
            async () => {
              // The secret reaches the command as an environment variable, so it never appears on screen.
              const events = EVENTS.map((event) => `-f 'events[]=${event}'`).join(" ")
              const { output } = await terminal.run(
                `gh api ${api}/hooks -f name=web -F active=true -f 'config[url]=${tunnelUrl}/api/github/events' ` +
                  `-f 'config[content_type]=json' -f 'config[secret]='"$ENGINE_GITHUB_WEBHOOK_SECRET" ${events} --jq .id`,
              )
              const id = output.trim().split("\n").at(-1)!.trim()
              expect(id, "GitHub answers with the new webhook's number").toMatch(/^\d+$/)
              await show("benchmark__terminal--webhook.png", terminal, testInfo, await terminal.row(new RegExp(`^${id}$`)), "The webhook's number")
              return id
            },
          )

          const hookPath = `${api}/hooks/${hook}`
          await step(
            "Check that it works. Ask GitHub to send a test message. GitHub keeps a log of every message it sends and the answer it got, so you can see whether OpenEngine accepted it. `<webhook number>` is the number the last command printed.\n\n" +
              "```sh\ngh api -X POST repos/<your-account>/" + BENCH_REPO + "/hooks/<webhook number>/pings\n" +
              "gh api repos/<your-account>/" + BENCH_REPO + "/hooks/<webhook number>/deliveries --jq '.[] | \"\\(.event) \\(.status_code)\"'\n```",
            async () => {
              await terminal.run(`gh api -X POST ${hookPath}/pings`)
              // GitHub's own log of what OpenEngine answered: 200 means it passed the signature check.
              const deliveries = async () => JSON.parse(await gh("api", `${hookPath}/deliveries`)) as { status_code: number }[]
              // GitHub now and then records nothing for a ping, not even a failure, so ask once more if it is still silent
              // after half a minute. An answer other than 200 still fails below.
              let retried = false
              await expect
                .poll(async () => (await deliveries()).length, { timeout: 30_000 })
                .toBeGreaterThan(0)
                .catch(() => {
                  retried = true
                  return gh("api", "-X", "POST", `${hookPath}/pings`)
                })
              // How often GitHub needs asking twice is a fact about the run, shown in its summary.
              testInfo.annotations.push({ type: "ping-retried", description: retried ? "yes" : "no" })
              await expect
                .poll(async () => (await deliveries()).map((d) => d.status_code), { timeout: 90_000, message: "waiting for GitHub to record OpenEngine's answer" })
                .toContain(200)
              await terminal.run(`gh api ${hookPath}/deliveries --jq '.[] | "\\(.event) \\(.status_code)"'`)
              await show("benchmark__terminal--webhook-accepted.png", terminal, testInfo, await terminal.row(/ping 200/), "OpenEngine accepted the message")
            },
          )

          await step(
            "A message that isn't signed with the secret is refused, so nobody else can send OpenEngine work. Send one yourself to see it: the answer is 401, which means \"not allowed\".\n\n" +
              "```sh\ncurl -s -o /dev/null -w '%{http_code}\\n' -X POST https://<your tunnel address>/api/github/events\n```",
            async () => {
              const { output } = await terminal.run(`curl -s -o /dev/null -w '%{http_code}\\n' -X POST ${tunnelUrl}/api/github/events`)
              expect(output.trim().split("\n").at(-1)).toBe("401")
              await show("benchmark__terminal--webhook-rejected.png", terminal, testInfo, await terminal.row(/^401$/), "OpenEngine refuses it")
            },
          )
        } finally {
          // Stopped here as well as by teardown, so a run that passes leaves nothing running.
          await terminal.run("oe daemon stop", { timeout: 120_000 }).catch(() => {})
        }
      })
    },
  )
})
