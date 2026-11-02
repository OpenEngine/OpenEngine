import { randomBytes } from "node:crypto"
import { expect } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { Terminal, test } from "../../fixtures/terminal.ts"
import { BENCH_REPO, BENCH_TOPIC, containerSession, getRepo, gh, whoami } from "../../support/benchmark.ts"
import { issues } from "../../fixtures/calculator/issues.ts"
import { milestone } from "../../fixtures/calculator/milestone.ts"
import { driveMilestone, type Budget } from "../../support/driver.ts"
import { show, step } from "../../support/show.ts"
import { expectDisplay, key, press } from "../../support/verifier.ts"
import { launchQuickTunnel } from "../../support/quick-tunnel.ts"
import { benchmarkCategory } from "../docCategories"

// The end-to-end benchmark (docs/plans/e2e-benchmark.md). It runs only through `npm run oe-benchmark`,
// never `npm test` or `npm run doc`, because of its tag. The "setup" project in
// playwright-benchmark.config.ts has already cleared the last run's leftovers by the time it starts.
// Each issue adds its steps to the journey below, in order.

/** The graph the journey makes from `tdd-guide` and then runs. */
const GRAPH = "calculator"

/**
 * What a run, and the whole benchmark, may spend or take. A run over its cap is cancelled and recorded as a failure.
 * Set from the baseline run, which cost about $1.57 as the daemon counts it, at most $0.26 for a run: about ten times
 * that, enough to stop a run that has gone wrong. The daemon's figure is an estimate on a subscription, so the minutes
 * are the cap that matters most.
 */
const BUDGET: Budget = { perRunUsd: 5, totalUsd: 25, perRunMinutes: 60 }

/**
 * The runners, fixed so runs compare: Claude picks the issue, writes the tests and writes the code, and Codex reviews the
 * change and consolidates the reviews, so neither model reviews its own work. Passed as inputs to the graph and to the
 * loop, which use Claude for all four otherwise. Splitting the work also keeps one subscription from taking all of it.
 */
const RUNNERS = ["runner=claude", "reviewer=codex", "implementer=claude", "reranker=codex"]
const runnerInputs = RUNNERS.map((input) => `--input ${input}`).join(" ")

/**
 * A model for every step of each runner, from `OE_BENCH_CLAUDE_MODEL` and `OE_BENCH_CODEX_MODEL`, for runs that are about
 * whether the benchmark works and not about how good OpenEngine is on its own models, and that must not use up a
 * subscription: `haiku` and `gpt-6-luna` are the cheapest (Claude's adapter takes the aliases `haiku`, `sonnet` and
 * `opus`, not full model IDs; `codex debug models` lists Codex's). Unset, a runner's steps use OpenEngine's own tiers
 * (cheapest, default and elevated), which is what a baseline measures.
 */
const MODELS = { claude: process.env.OE_BENCH_CLAUDE_MODEL, codex: process.env.OE_BENCH_CODEX_MODEL }

/** How many of the calculator's issues to seed: all of them, or the first few with `OE_BENCH_ISSUES`, for a cheap trial. */
const SEEDED = Number(process.env.OE_BENCH_ISSUES ?? issues.length)

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
  test.describe.serial(
    withDocMeta("Build Software With OpenEngine", {
      description: "A step-by-step guide to building software with OpenEngine: install it, give it a list of GitHub issues, and watch it build the project, using a small calculator as the example.",
      sidebar_position: 1,
      slug: "/start-to-finish/build-software",
    }),
    () => {
      test("Step by step", async ({ terminal, context }, testInfo) => {
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
          "Install it. The installer builds OpenEngine and puts the `oe` command in your home folder. It takes under a minute. `--no-start` leaves OpenEngine stopped until you start it below.\n\n```sh\nscripts/install-local.sh --no-start\noe --version\n```",
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

        await step(
          "Before opening a tunnel, protect OpenEngine with a token. A tunnel makes it reachable outside your network. Make a secret token and save it in a file only you can read. Export it too, so commands in this terminal can authenticate. Keep this terminal open.\n\n```sh\nexport ENGINE_SERVICE_TOKEN=$(openssl rand -hex 32)\n(umask 077; touch ~/.config/openengine/.env)\nchmod 600 ~/.config/openengine/.env\nprintf 'ENGINE_SERVICE_TOKEN=%s\\n' \"$ENGINE_SERVICE_TOKEN\" >> ~/.config/openengine/.env\nls -l ~/.config/openengine/.env\n```",
          async () => {
            await terminal.run("export ENGINE_SERVICE_TOKEN=$(openssl rand -hex 32)")
            await terminal.run("(umask 077; touch ~/.config/openengine/.env)")
            await terminal.run("chmod 600 ~/.config/openengine/.env")
            await terminal.run(`printf 'ENGINE_SERVICE_TOKEN=%s\\n' "$ENGINE_SERVICE_TOKEN" >> ~/.config/openengine/.env`)
            await terminal.run("ls -l ~/.config/openengine/.env")
            await show("benchmark__terminal--token-permissions.png", terminal, testInfo, await terminal.row(/-rw-------/), "Only you can read the token file")
          },
        )

        await step(
          "Repository setup uses a separate onboarding token because it can grant repository trust. Keep it private too.\n\n```sh\nexport ENGINE_ONBOARDING_TOKEN=$(openssl rand -hex 32)\nprintf 'ENGINE_ONBOARDING_TOKEN=%s\\n' \"$ENGINE_ONBOARDING_TOKEN\" >> ~/.config/openengine/.env\noe backend add onboarding http://127.0.0.1:4364 --token-env ENGINE_ONBOARDING_TOKEN\n```",
          async () => {
            await terminal.run("export ENGINE_ONBOARDING_TOKEN=$(openssl rand -hex 32)")
            await terminal.run(`printf 'ENGINE_ONBOARDING_TOKEN=%s\\n' "$ENGINE_ONBOARDING_TOKEN" >> ~/.config/openengine/.env`)
            await terminal.run("oe backend add onboarding http://127.0.0.1:4364 --token-env ENGINE_ONBOARDING_TOKEN")
            await show("benchmark__terminal--onboarding-backend.png", terminal, testInfo, await terminal.row(/onboarding/), "A separate credential for repository setup")
          },
        )

        const tunnelCommand = `cloudflared tunnel --url http://127.0.0.1:${port}`
        const tunnelUrl = await step(
          "OpenEngine runs on your computer, and GitHub can't reach your computer directly. A Cloudflare quick tunnel gives it a temporary public address. Open a second terminal for it, because the tunnel keeps running, and start the tunnel there. It needs no Cloudflare account.\n\n" +
            "```sh\n" + tunnelCommand + "\n```",
          async () => {
            // Tall enough that the address stays in view while the tunnel's log fills the screen.
            const tunnel = await Terminal.open(await context.newPage(), containerSession(), { checkoutEngine: false, shell: true })
            return launchQuickTunnel(tunnel, port, {
              onAddress: async (url) => {
                await show("benchmark__terminal--tunnel.png", tunnel, testInfo, await tunnel.row(new RegExp(url.replace(/\./g, "\\."))), "Your temporary public address")
              },
            })

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
          "OpenEngine reads the webhook secret from a file named `.env`, kept next to `engine.toml` and readable only by you. Here the secret is in an environment variable named `ENGINE_GITHUB_WEBHOOK_SECRET`, so it never appears on screen.\n\n```sh\nprintf 'ENGINE_GITHUB_WEBHOOK_SECRET=%s\\n' \"$ENGINE_GITHUB_WEBHOOK_SECRET\" >> ~/.config/openengine/.env\nchmod 600 ~/.config/openengine/.env\nls -la ~/.config/openengine\n```",
          async () => {
            await terminal.run(`printf 'ENGINE_GITHUB_WEBHOOK_SECRET=%s\\n' "$ENGINE_GITHUB_WEBHOOK_SECRET" >> ~/.config/openengine/.env`)
            await terminal.run("chmod 600 ~/.config/openengine/.env")
            await terminal.run("ls -la ~/.config/openengine")
            await show("benchmark__terminal--secret-file.png", terminal, testInfo, await terminal.row(/-rw-------.* \.env$/), "The secret is in a file only you can read")
          },
        )

        await step("[nodoc] Each runner's steps use the model this run was asked to use", async () => {
          for (const [runner, model] of Object.entries(MODELS)) {
            testInfo.annotations.push({ type: `${runner}-model`, description: model ?? "OpenEngine's own tiers" })
            if (!model) continue
            // Each tier of the runner's steps becomes the one model. Steps read the tiers when they start.
            const tiers = ["cheapest", "default", "elevated"].map((tier) => `${tier} = "${model}"`).join(", ")
            await terminal.run(`printf '\\n[runners.${runner}]\\nmodels = { ${tiers} }\\n' >> ~/.config/openengine/engine.toml`)
          }
          await terminal.run("clear")
        })

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
            "Connect OpenEngine to GitHub. It uses `gh`, GitHub's own tool for the terminal, so there is nothing to paste. If you don't have it yet, [install it](https://github.com/cli/cli#installation) and [sign in](https://cli.github.com/manual/gh_auth_login) first.\n\n```sh\noe connect gh\n```",
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
            "The graph you make next uses a second coding agent, Codex, to review the change and consolidate the reviews, so check that it is signed in as well.\n\n```sh\ncodex login status\n```",
            async () => {
              // Found here, like Claude's sign-in: a run that starts without it fails at the step that implements.
              await terminal.run("codex login status").catch(() => {
                throw new Error("Codex is not signed in where OpenEngine runs. Sign in with `codex login`, then run the benchmark again.")
              })
              await show("benchmark__terminal--codex-login.png", terminal, testInfo, await terminal.row(/logged in/i), "Codex is signed in")
            },
          )

          await step(
            "OpenEngine works through graphs, the steps it follows for a job. It comes with `tdd-guide`, which works an issue test first, and you can copy a graph and change it to fit your project. A graph is a file, so copy this one.\n\n```sh\ncp docs/examples/graphs/tdd-guide.yaml ~/" + GRAPH + ".yaml\n```",
            async () => {
              await terminal.run(`cp docs/examples/graphs/tdd-guide.yaml ~/${GRAPH}.yaml`)
            },
          )

          await step(
            "Open the copy in the text editor. The graph is named at the top, so give it a name of its own. Every pull request is reviewed from several angles, each a line in the review step, and a small project doesn't need seven. Keep security as it is, and combine the rest into one reviewer, Quality, that looks for bugs, slow code, needless code and repeated patterns.\n\n" +
              "```sh\nvim ~/" + GRAPH + ".yaml\n```\n\nThen type these, each followed by Enter, and save with `:wq`:\n\n" +
              "```vim\n:%s/^name: tdd-guide$/name: " + GRAPH + "/\n:/id: guide/-1;/^    prompt: |/-1d\n:g/^      - {facet: \\(performance\\|conciseness\\|dryness\\)}$/d\n:%s/^      - {facet: bugs}$/      - {facet: {id: quality, name: Quality, focus: \"Bugs, whether the change does what the issue asks, slow code and needless code. Also repetition: read the whole file the change lands in, and when it adds another copy of a pattern the file already repeats, count the copies and name the shared form (a helper, a lookup table or an id).\"}}/\n```",
            async () => {
              await terminal.type(`vim ~/${GRAPH}.yaml`)
              await terminal.press("Enter")
              await terminal.waitForText(new RegExp(`${GRAPH}\\.yaml"`))
              await terminal.type(`:%s/^name: tdd-guide$/name: ${GRAPH}/`)
              await terminal.press("Enter")
              await terminal.type("/facet: security")
              await terminal.press("Enter")
              await show("benchmark__terminal--review-angles.png", terminal, testInfo, await terminal.row(/facet: dryness/), "The review step lists its angles, one per line")
              // The guides and docs angles span several lines: from the line before their id to the line before the prompt.
              await terminal.type(":/id: guide/-1;/^    prompt: |/-1d")
              await terminal.press("Enter")
              await terminal.type(":g/^      - {facet: \\(performance\\|conciseness\\|dryness\\)}$/d")
              await terminal.press("Enter")
              await terminal.type(':%s/^      - {facet: bugs}$/      - {facet: {id: quality, name: Quality, focus: \"Bugs, whether the change does what the issue asks, slow code and needless code. Also repetition: read the whole file the change lands in, and when it adds another copy of a pattern the file already repeats, count the copies and name the shared form (a helper, a lookup table or an id).\"}}/')
              await terminal.press("Enter")
              await terminal.type("/facet: security")
              await terminal.press("Enter")
              await show("benchmark__terminal--review-angles-kept.png", terminal, testInfo, await terminal.row(/id: quality/), "Security, and one reviewer for everything else, are left")
              await terminal.type(":wq")
              await terminal.press("Enter")
              await terminal.waitForPrompt()
              await terminal.run("clear")
            },
          )

          await step(
            "Register your graph, then list what is registered.\n\n```sh\noe graph add ~/" + GRAPH + ".yaml\noe graphs\n```",
            async () => {
              await terminal.run(`oe graph add ~/${GRAPH}.yaml`)
              await terminal.run("oe graphs")
              await show("benchmark__terminal--graph-registered.png", terminal, testInfo, await terminal.row(new RegExp(`^${GRAPH}\\s`)), `${GRAPH} is registered`)
            },
          )

          await step(
            "Your project lives in a GitHub repository. Create one with the `gh` command from the last step. It is created under the account `gh` is signed in as.\n\n" +
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
            "Tell OpenEngine about the repository. `oe repo add` downloads it onto your computer and remembers where it is, so OpenEngine knows which folder to work in. `--trust` lets its agents run commands in this repository without asking each time. Only the onboarding token can grant that, which is why this command uses the onboarding backend. It takes effect straight away.\n\n" +
              "```sh\noe repo add --backend onboarding https://github.com/<your-account>/" + BENCH_REPO + " --path ~/" + BENCH_REPO + " --trust\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`oe repo add --backend onboarding https://github.com/${repo} --path ~/${BENCH_REPO} --trust`, { timeout: 180_000 })
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
              // A setting just changed can read back as it was for a moment, so each check waits for GitHub to catch up.
              await expect.poll(() => gh("api", `${api}/pages`, "--jq", ".build_type"), { timeout: 30_000, message: "Pages publishes from GitHub Actions" }).toBe("workflow\n")
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
              await expect
                .poll(async () => JSON.parse(await gh("api", `${api}/actions/permissions/workflow`)), { timeout: 30_000, message: "workflows can write, and can approve pull requests" })
                .toEqual({ default_workflow_permissions: "write", can_approve_pull_request_reviews: true })
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
              await expect
                .poll(async () => JSON.parse(await gh("api", api)), { timeout: 30_000, message: "squash merging is allowed, titled by the pull request" })
                .toMatchObject({ allow_squash_merge: true, squash_merge_commit_title: "PR_TITLE" })
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
              for (const issue of issues.slice(0, SEEDED)) {
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
              // `gh issue list --milestone` reads GitHub's search index, which trails the issues just created.
              await expect
                .poll(async () => (JSON.parse(await gh("issue", "list", "--repo", repo, "--milestone", milestone.title, "--state", "open", "--json", "number")) as unknown[]).length, {
                  timeout: 90_000,
                  message: "waiting for GitHub's search to list the issues",
                })
                .toBe(SEEDED)
              await terminal.run(`gh issue list --repo ${repo} --milestone ${milestone.title} --state open --limit 30`)
              await show("benchmark__terminal--issues.png", terminal, testInfo, await terminal.row(/Set up the project/), "The calculator's issues")
              // GitHub's listing can trail the issues just created, so wait for it to show them all.
              const listIssues = async () => JSON.parse(await gh("api", `${api}/issues?milestone=${number}&state=open&per_page=100`)) as { number: number; body: string }[]
              await expect.poll(async () => (await listIssues()).length, { timeout: 30_000, message: "every issue is in the milestone" }).toBe(SEEDED)
              // Each issue after the first depends on the one before it.
              if (numbers.length > 1) {
                expect(await gh("api", `${api}/issues/${numbers.at(-1)}/dependencies/blocked_by`, "--jq", "[.[].number]"), "GitHub shows the last issue as blocked by the one before it").toContain(`${numbers.at(-2)}`)
                expect((await listIssues()).find((issue) => issue.number === Number(numbers.at(-1)))?.body, "the last issue links to the one before it").toContain(`Depends on #${numbers.at(-2)}`)
              }
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
              // GitHub delivers the ping at once, but its list of deliveries can take minutes to show it, so wait for the
              // list rather than send the ping again. An answer other than 200 still fails.
              const asked = Date.now()
              await expect
                .poll(async () => (await deliveries()).map((d) => d.status_code), { timeout: 300_000, intervals: [5_000], message: "waiting for GitHub to list OpenEngine's answer" })
                .toContain(200)
              // How long the list took is a fact about the run, shown in its summary.
              testInfo.annotations.push({ type: "delivery-log-seconds", description: String(Math.round((Date.now() - asked) / 1000)) })
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

          await step(
            "Now give OpenEngine its first issue. `oe graph run` starts a graph on a repository. `" + GRAPH + "` works one issue test first: with a milestone to pick from, it takes the next issue that nothing is waiting on.\n\n" +
              "```sh\noe graph run " + GRAPH + " --repo ~/" + BENCH_REPO + " --input milestone=Calculator \\\n  " + runnerInputs + " --no-watch\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`oe graph run ${GRAPH} --repo ~/${BENCH_REPO} --input milestone=${milestone.title} ${runnerInputs} --no-watch`, { timeout: 120_000 })
              await show("benchmark__terminal--first-issue-started.png", terminal, testInfo, await terminal.row(/run-[0-9a-f]+/i), "OpenEngine has started on the first issue")
            },
          )

          const first = await step("[nodoc] The first run is worked to the end: its review approved and its pull request merged", async () => {
            // The people's part of the work, done by the harness: a run waits for a person to approve it.
            return driveMilestone({ repo, milestone: milestone.title, graph: GRAPH, budget: BUDGET, until: "first-run-done", onProgress: (line) => console.log(`[first run] ${line}`) })
          })

          // Nothing after this works if the first run didn't: stop, with what went wrong, rather than hand it to a loop.
          expect(first.failures, "the first run failed").toEqual([])

          await step(
            "OpenEngine can keep going on its own. A loop starts a run on a schedule, so each issue is picked up once the one before it is done. Set a limit on what it may spend and on how many pull requests it may open.\n\n" +
              "```sh\noe loop add " + GRAPH + " --repo ~/" + BENCH_REPO + " --input milestone=Calculator \\\n  " + runnerInputs + " \\\n  --every 1m --max-spend 25 --max-prs 25 --no-run-now\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(
                `oe loop add ${GRAPH} --repo ~/${BENCH_REPO} --input milestone=${milestone.title} ${runnerInputs} --every 1m --max-spend ${BUDGET.totalUsd} --max-prs 25 --no-run-now`,
                { timeout: 120_000 },
              )
              await terminal.run("oe loops")
              await show("benchmark__terminal--loop-running.png", terminal, testInfo, await terminal.row(new RegExp(`^${GRAPH}\\s`)), "The loop is running")
            },
          )

          const rest = await step("[nodoc] The loop's runs are worked to the end, until no issue in the milestone is open", async () => {
            return driveMilestone({ repo, milestone: milestone.title, graph: GRAPH, budget: BUDGET, until: "milestone-done", onProgress: (line) => console.log(`[loop] ${line}`) })
          })

          await step(
            "When every issue is done, pause the loop so it stops looking for work.\n\n" +
              "```sh\noe loop pause " + GRAPH + "\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(`oe loop pause ${GRAPH}`)
              await show("benchmark__terminal--loop-paused.png", terminal, testInfo, await terminal.row(/paused/i), "The loop is paused")
            },
          )

          await step(
            "That is the calculator built. Every issue in the milestone is closed, and every pull request that closed one is merged.\n\n" +
              "```sh\ngh issue list --repo <your-account>/" + BENCH_REPO + " --milestone " + milestone.title + " --state all \\\n  --json number,state,title --jq '.[] | \"#\\(.number) \\(.state) \\(.title)\"'\ngh pr list --repo <your-account>/" + BENCH_REPO + " --state merged \\\n  --json number,title --jq '.[] | \"#\\(.number) \\(.title)\"'\n```",
            async () => {
              await terminal.run("clear")
              await terminal.run(
                `gh issue list --repo ${repo} --milestone ${milestone.title} --state all --limit 30 --json number,state,title --jq '.[] | "#\\(.number) \\(.state) \\(.title)"'`,
              )
              await terminal.run(`gh pr list --repo ${repo} --state merged --limit 30 --json number,title --jq '.[] | "#\\(.number) \\(.title)"'`)
              await expect
                .poll(async () => (JSON.parse(await gh("issue", "list", "--repo", repo, "--milestone", milestone.title, "--state", "open", "--json", "number")) as unknown[]).length, {
                  message: "no issue in the milestone is still open",
                  timeout: 60_000,
                })
                .toBe(0)
              await show("benchmark__terminal--issues-closed.png", terminal, testInfo, await terminal.row(/CLOSED/), "Every issue is closed")
            },
          )

          // What the work cost and how it went, for the report.
          // The second phase sees the first run again, so a run counts once, as the later phase last saw it.
          const runs = [...new Map([...first.runs, ...rest.runs].map((run) => [run.runId, run])).values()]
          testInfo.annotations.push(
            { type: "runs", description: String(runs.length) },
            { type: "rate-limited-runs", description: String(runs.filter((run) => run.status === "rate-limited").length) },
            { type: "failed-runs", description: String(runs.filter((run) => run.status !== "completed" && run.status !== "rate-limited").length) },
            { type: "total-cost-usd", description: runs.reduce((sum, run) => sum + run.costUsd, 0).toFixed(2) },
          )
          await testInfo.attach("runs.json", { body: JSON.stringify(runs, null, 2), contentType: "application/json" })
          expect([...first.failures, ...rest.failures], "no run failed or went over budget").toEqual([])
        } finally {
          // Stopped here as well as by teardown, so a run that passes leaves nothing running.
          await terminal.run("oe daemon stop", { timeout: 120_000 }).catch(() => {})
        }
      })

      // The benchmark's pass or fail for the calculator: a fixed check of what the journey built, written by a person and
      // never shown to the agents. It checks that the guide's run worked, so none of its steps are in the guide. It runs
      // right after the journey, and only if the journey passed, since the next run's setup deletes the repository.
      test("Verify the calculator", { tag: ["@verify"] }, async ({ page }) => {
        const url = `https://${await whoami()}.github.io/${BENCH_REPO}/`

        await step("The calculator is deployed with all its keys", async () => {
          // The deploy follows the last merge by a minute or so. The last key to be built is ÷, so wait until the page has it.
          await expect(async () => {
            await page.goto(url)
            await expect(key(page, "÷")).toBeVisible({ timeout: 2_000 })
          }).toPass({ timeout: 10 * 60_000, intervals: [20_000] })
        })

        await step("It adds", async () => {
          await page.goto(url)
          await press(page, "1+1=")
          await expectDisplay(page, "2")
        })

        await step("It subtracts", async () => {
          await page.goto(url)
          await press(page, "5-3=")
          await expectDisplay(page, "2")
        })

        await step("It multiplies", async () => {
          await page.goto(url)
          await press(page, "3×4=")
          await expectDisplay(page, "12")
        })

        await step("It divides", async () => {
          await page.goto(url)
          await press(page, "10÷2=")
          await expectDisplay(page, "5")
        })

        await step("It says Error when dividing by zero, and carries on", async () => {
          await page.goto(url)
          await press(page, "10÷0=")
          await expectDisplay(page, "Error")
          await press(page, "C7")
          await expectDisplay(page, "7")
        })

        await step("It clears the display", async () => {
          await page.goto(url)
          await press(page, "77C")
          await expectDisplay(page, "0")
        })
      })
    },
  )
})
