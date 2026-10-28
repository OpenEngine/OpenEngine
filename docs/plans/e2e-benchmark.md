# True end-to-end benchmark

One real run of OpenEngine, start to finish, with real models, real GitHub and a
real deploy. It answers two questions: does OpenEngine work end to end, and what
does it cost in tokens, dollars and time to do so with a given model.

It is run before each release, not on every PR. It is slow, it spends real
money, and agent runs are noisy.

It is also the regression check for the v2 rewrite. The journey is run against
v1 first to set the baseline, then against v2, so we know the rewrite did not
break what v1 could do. That means the journey only touches interfaces v2 is
meant to keep (the CLI and GitHub), never v1 internals, and the v1 results are
kept as the comparison point.

## The journey

OpenEngine does not do planning yet, so the harness does the planning ahead of
time. It gives OpenEngine an empty repository seeded with a fixed, ordered set of
issues that spell out how to build the calculator web app (add, subtract,
multiply, divide), for example: scaffold the app, one issue per operation, divide
by zero, clear, CI, commitlint, docs, deploy to GitHub Pages, and automated
semver releases with a generated changelog. Each issue is self-contained and
builds on the merged result of the one before it.

The issues sit in a milestone, and the `tdd-guide` graph works the milestone
until it is done. Working an issue, opening its PR, CI/CD and the deploy are
OpenEngine's job. We do not care how it gets there, only that it does.

### Run sequence

One config runs the whole benchmark. The report (see below) is configured up
front and collected as the run goes, not a step of its own.

0. **Teardown.** Delete the calculator repo, the deployed site and the
   container the last run happened in, with the daemon and tunnel running in it.
1. **Clone OpenEngine** at the ref under test (the release commit, or the v1
   tag for the baseline), and record its SHA.
2. **Set up OpenEngine.** Configure GitHub CLI access with the bench account's
   token and isolated `gh_config_dir`. Reuse a pre-registered benchmark GitHub
   App if app-backed webhooks are wanted; current setup does not create one
   (see the #1137 findings below). Screenshot where each secret goes, with the secret masked by a
   test2doc highlight annotation. Start the Cloudflare Tunnel. Register the `tdd-guide` graph.
   Check every runner is signed in (`oe doctor` for Claude, `codex login status`
   for Codex), so a missing sign-in fails here rather than minutes into a run.
3. **Create the calculator repo** under the account `gh` is logged in as, and
   clone it locally.
4. **Connect the repo to OpenEngine.** Grant the existing app access to the new
   repo or add a repository webhook, and
   apply the repository settings below, including turning on Pages. The deploy
   workflow itself is one of the seeded issues.
5. **Seed** the milestone and its issues.
6. **Run `tdd-guide`** on the milestone until it is done, through both CLI entry
   points:
   1. **One run.** Work the first issue with `oe graph run tdd-guide --repo
      PATH -i milestone=NAME --trust`, so a single run is covered end to end.
      `--repo` takes the local clone (or a configured name), not `OWNER/REPO`,
      and `--trust` lets agents run commands without asking.
   2. **A loop.** Then `oe loop add tdd-guide` with `--every` short enough to
      keep the run moving (the graph's default is 1h), and `--max-spend` and
      `--max-prs` as the budget cap. The loop works the rest of the milestone.
      It does not stop on its own, so the harness pauses it once the milestone
      has no open issues.

   Each run works one issue and ends at a `human_review` step, and the graph
   never merges, so for every run the harness approves `human_review` and
   merges the PR, which closes the issue and unblocks the next. A loop run that
   starts before the previous PR merges finds no unblocked issue and ends. The
   guide shows how PRs work, with reviews and impact analysis. A steer is
   optional and only at a fixed, scripted point, since a run cannot wait for a
   good example.
7. **Verify** the calculator on Pages.
8. **Docs and release checks** on the calculator's own docs and releases.
9. **Commit the guide.**

After the run, at no fixed time but before each release, a person checks the
deployed calculator.

The harness owns only what must stay outside the thing being measured:

- **Seed.** Copy the milestone and its issues into the fresh repo. The issues are written once and
  committed as plain files (title and body, in order) under the `e2e/`
  fixtures. Seeding is a copy through the GitHub API, with no model involved,
  so it costs no tokens and every trial gets identical issues.

- **Reset.** Delete and recreate the bench repo, wipe local OpenEngine state,
  and tear down the deployed site, so every run starts clean and is idempotent.
  The repo lives under the account `gh` is logged in as, which owns it and so
  can delete it (its token needs the `delete_repo` scope). Teardown refuses any
  repo without the bench prefix and `oe-bench` topic. Then apply the repository
  settings below.
- **Merge loop.** Approve each run's `human_review` step through the API
  (`POST /runs/{id}/approvals/{approvalId}`), then merge its PR once CI is green
  (`gh pr merge --squash`). It merges whatever the impact rating, including
  Red, and records the rating in the report. Approve first: OpenEngine ignores
  the merge webhook when the merger is its own login, by design, so a merge by
  the same account does not release `human_review`. If the issue is still open
  after the merge, the harness records that and closes it.
- **Verifier.** A fixed Playwright spec the agent never sees. It runs against the
  Pages URL and checks add, subtract, multiply, divide, divide by zero and
  clear. It finds things by visible text (`getByText`, or role and name), not
  `data-testid`, so the issues' contract is only the Pages URL and plain
  wording: keys labelled `7`, `+`, `-`, `×`, `÷`, `=` and `C`, and a visible
  display. Nothing else constrains how OpenEngine builds, including how it
  tests its own work.
- **Docs check.** The calculator repo must document itself: a README that says
  how to run, test and deploy it. The verifier checks the file exists and covers
  those three, and the person checking the final output reads it.
- **Release check.** CI cuts releases on its own: merges to the default branch
  produce a semver tag, a GitHub release and a changelog generated from the
  commits. The check asserts a release exists with a semver tag and a
  `CHANGELOG.md` entry that lists the merged changes. The issue may name a tool
  (for example release-please or semantic-release), or leave it to OpenEngine.
  Changelogs are built from Conventional Commits, and a squash merge uses the PR
  title as the commit, so PR titles must follow Conventional Commits. A commitlint
  check in CI enforces this on the PR title (and on commits), so a bad title fails
  CI instead of quietly breaking the changelog. If the tool opens a release PR,
  the merge loop merges it too.
- **Report.** See below.

The verifier and the issues are written and reviewed by a person, outside any
graph run. OpenEngine's own acceptance tests mean it partly grades itself, so
two checks sit outside it:

- The verifier is the only pass or fail. It never sees the agent's tests.
- A person checks the deployed calculator after the run.

### Repository settings the user must enable

A fresh repo does not have what the journey needs. These are prerequisites, not
something OpenEngine is expected to discover, and they are documented as setup
steps for anyone running OpenEngine against a real repo:

- **GitHub Pages** enabled, with the source set to GitHub Actions, so the deploy
  issue's workflow can publish.
- **Actions workflow permissions** set to read and write, with "Allow GitHub
  Actions to create and approve pull requests" on.
- **Squash merging** allowed, since the merge loop uses `gh pr merge --squash`.
- **Webhook delivery** to OpenEngine, for the CI-wake step. OpenEngine runs on
  the harness machine, so GitHub reaches it through a Cloudflare Tunnel
  (`cloudflared`). Use a quick tunnel, which gets a new random URL each run and
  needs no Cloudflare account or domain. Setup starts it, reads the URL, and
  points the webhook at it (the app's webhook config, or the repo webhook), and
  teardown stops it. A fresh URL each run means nothing outside a run knows
  where to send traffic. The real protection is still the webhook secret:
  OpenEngine checks each delivery's signature and drops anything unsigned.
  Three things the first run of this found:
  OpenEngine answers only requests whose host it knows (loopback and `public_url`),
  so it must start after the tunnel with the tunnel's address as `public_url`, and
  the port is chosen first because the tunnel needs it; the tunnel runs with an empty
  `HOME`, since a named tunnel's `~/.cloudflared/config.yml` makes a quick tunnel
  answer 404; and the terminal's `gh` and the harness's must be the same account
  (a `gh` that picks its account by directory can differ), so the run checks that
  before creating anything. A machine with one `gh` login needs nothing more; where
  they can differ, set `GH_CONFIG_DIR` to the bench account's config directory.

The harness applies these through the API right after recreating the repo, so
every trial is automated. Because the harness is doing what a user would do by
hand, a failure here points at the setup docs, not at the model. Each setting
is its own test step with a screenshot, so the committed guide is the setup doc
and cannot drift from what the harness does.

## Layers

One journey, built one layer at a time. Each later layer swaps a step of the
journey to a different interface and leaves the rest alone.

1. **CLI.** The whole journey, CLI only: `gh` auth setup, seed the issues, run
   `tdd-guide` on the milestone, inspect results. This layer also carries the GitHub
   integration the journey needs anyway (PRs, CI, merge).
2. **GitHub integration.** Add assertions about issue and PR linking and webhook
   delivery, plus validating the GitHub CLI setup.
3. **Slack (stretch).** Run-complete and needs-input notifications, and a
   Slack-started run as a variant. Slack may be hard to automate, so this layer
   is optional. **Try it first**: before dropping it, spend a short spike
   finding out whether a test Slack workspace and bot can be driven from the
   harness. Record the outcome here either way.

The web UI is not a layer. It is being redone as part of v2, so a v1 web UI layer
would test something v2 is replacing and could not serve as a regression
baseline. Once the new web UI exists, add a layer for it that starts a run and
inspects the run, cost and per-node tokens.

## Product gaps this exposes

- No step merges or auto-merges PRs (the harness covers it for now).
- No wall-clock or run-level benchmark report. Token and dollar totals already
  exist in `packages/graph_runtime/src/engine/graph_runtime/usage.py`.

## Real models

- Runs use real agents (`E2E_AGENT=real` semantics), real GitHub and a real
  Pages deploy. There is no stubbed variant, so nothing about the journey is
  tested on PRs. The harness is only exercised by benchmark runs, and the
  verifier is checked against a hand-written calculator first.
- Runs are manual or on-demand, never on PR CI. Trigger them from a
  `workflow_dispatch` workflow or locally.
- Each run has a **budget cap** (dollars and wall-clock). Hitting a cap kills the
  run and records a failure, so a stuck run cannot burn money without limit.
- Runners are fixed for now, set through `tdd-guide`'s inputs: Claude for
  planning and review (`runner`, which picks the issue and writes the tests, and
  `reviewer`), Codex for implementation and reranking (`implementer` and
  `reranker`). Comparing other runners comes later.
- Results are noisy, so run several trials and compare medians.
- Credentials: the GitHub account `gh` is logged in as, with `repo` and
  `delete_repo` scopes, and model credentials for the runners under test.

### Release gate

`docs/releases.md` already gates a release on the most recent scheduled
`ACP compatibility` run being green and recent. The benchmark could gate the
same way: the most recent benchmark run on the release commit's lineage must
have passed within an agreed window. Because a single run is noisy, the gate
should be a pass threshold across trials (for example, 2 of 3), decided once the
baseline exists.

## Report

The measurements for a run, not its artifacts. The artifacts are the calculator
repo, its PRs and docs, the deployed site, and the guide. One JSON record per run:

- runner and model per `tdd-guide` role, OpenEngine git SHA, trial number
- pass or fail per verifier case, and an overall pass, partial, fail or timeout
- wall-clock per phase
- tokens and dollars, total, per node, per `tdd-guide` role and per issue
- what OpenEngine produced: PR count, CI failures, retries and agent turns
  (recorded, not graded), and per issue whether its PR merged

Cost and tokens are what make a runner swap comparable: with the same issues
and verifier, a change in runner shows up as a change in tokens, dollars and
pass rate. The first runs with the fixed runners set that baseline.

`usage.py` already rolls up tokens (input, output, cache read and write) and
dollars per node from each run's `usage.updated` events. Three things to handle:

- Dollars are approximate. A cost the agent reports is used as-is, otherwise
  tokens are priced at list rates. An agent that reports nothing has unknown
  cost, not zero, and the totals say so. Compare tokens first, dollars second.
- The rollup lives in the web app's in-memory event log, so the harness reads
  each run's usage before teardown or a server restart loses it.
- The prices live in `usage.py`, so the recorded OpenEngine SHA pins them. A
  price change between runs is not a runner change.

## Test harness

The benchmark is a spec in the existing `e2e/` Playwright suite, not a separate
framework, so it reuses the per-test engine daemon and the terminal fixtures.

- **The CLI runs through xterm.js.** `e2e/fixtures/terminal.ts` already runs
  `engine …` in a real PTY in a browser page, so the CLI layer drives the same
  terminal the existing guides use.
- **Screenshots at every step.** Each `test.step` captures a screenshot, so a run
  leaves a visual record of the whole journey, including the failure point.
- **test2doc runs it.** `playwright-test2doc.config.ts` turns the steps and
  screenshots into a readable walkthrough of the run.
- **No practical timeout.** The suite's default is 10 s per action
  (`e2e/playwright.config.ts`). The benchmark overrides the test timeout with a
  very large value, or none, and relies on the harness's own dollar and
  wall-clock cap to stop a stuck run. Waits on external state (CI, the deploy)
  poll with their own bounded deadlines, so the cap is the only global limit.

### Logins that work under Playwright

Driving a real GitHub or Slack sign-in page from automation is fragile (2FA,
bot checks, magic links, SSO), so the approach is to avoid the interactive page
wherever the test is not about login:

- **GitHub.** The benchmark runs as whatever account `gh` is logged in as, read
  at the start with `gh api user`, so it can run on any machine that runs
  OpenEngine: rock-ai-bot on one Mac mini, OpenEngine-worker-bot on another.
  The harness and OpenEngine use the same account and token (`gh` auth and
  `gh_config_dir`), which needs no browser. Bench repos are created under that
  account's own user, so it owns them and can delete them, and nothing in an
  org has to grant it access. One account is enough because the harness
  approves `human_review` itself before merging. Testing that a person's merge
  releases `human_review` needs a second account and belongs to the GitHub
  integration layer. How CI provides the account is decided later. For
  browser-only steps, a Chromium virtual authenticator can export and import a
  passkey across fresh browsers, and GitHub accepted it in a manual trial (see
  the #1137 findings). Browser-only steps may sign in this way, from a local
  gitignored credential.
- **Slack.** Use a dedicated test workspace. Anything that does not need the
  Slack UI (posting a message to the bot, reading its replies) goes through the
  Slack API with a bot or user token. Screenshots of the Slack UI, if wanted,
  use a saved `storageState` from a one-time manual sign-in. This is the part
  most likely to be hard, which is why the layer is a spike first.
- Saved sessions and tokens are secrets: they live in CI secrets or a gitignored
  local file, never in the repo or in screenshots. The benchmark tests mask them
  in every screenshot they take: highlight the secret with a test2doc
  [highlight annotation](https://www.test2doc.com/docs/user-guide/screenshots/annotation#highlight-box-styling)
  and set `highlightFillStyle` to an opaque hex color, so the box covers the
  value instead of tinting it.

### GitHub setup spike (#1137)

Investigated from 2026-10-23 to 2026-10-26. Findings distinguish source inspection and documented
API behavior from a live GitHub browser trial.

**App registration.** Current `oe setup` installs and starts the daemon
(`apps/cli/src/engine/apps/cli/daemon.py`); `oe connect gh` selects GitHub CLI,
and `oe connect github` uses device authorization
(`apps/cli/src/engine/apps/cli/connect.py`). There is no app-manifest creation
flow in these setup paths. The assumption that current setup creates an app is
false. Implementing it is outside this spike.

GitHub's [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
could support a future setup: submit a form with JSON `manifest` to
`https://github.com/settings/apps/new` (or the organization equivalent) in a
signed-in browser, confirm the app name, and receive `code` at `redirect_url`.
Validate a random `state`, then exchange the code with
`POST /app-manifests/{code}/conversions` within one hour. The response contains
the app ID, private key, webhook secret and client secret. This automates the
configuration, but still requires browser registration and a separate app
installation. No app was created in this investigation.

**App lifecycle decision.** Reuse one dedicated benchmark app and its account
installation. GitHub documents [registration deletion through Settings](https://docs.github.com/en/apps/maintaining-github-apps/deleting-a-github-app),
and its [app REST endpoints](https://docs.github.com/en/rest/apps/apps) provide
`DELETE /app/installations/{installation_id}` to uninstall, not delete the app
registration. No supported REST registration-deletion endpoint was found; do
not depend on an undocumented browser endpoint or create an app per trial.

After repo recreation, grant access to its **new repository ID** with
[`PUT /user/installations/{installation_id}/repositories/{repository_id}`](https://docs.github.com/en/rest/apps/installations#add-a-repository-to-an-app-installation),
using an authorized repository admin credential with the endpoint's required
permissions. Preserve the installation rather than uninstalling and requiring
browser installation again. Update the quick tunnel URL through
[`PATCH /app/hook/config`](https://docs.github.com/en/rest/apps/webhooks#update-a-webhook-configuration-for-an-app)
using an app JWT. These lifecycle operations are documented, not live-tested
against a benchmark app. For the first CLI baseline, use the existing `gh`
provider and a repository webhook; it needs no GitHub browser session.

**Passkey method.** The spike used a throwaway Playwright script, not kept; the
setup step should reproduce these calls. Chromium launches with a Chrome
DevTools Protocol [WebAuthn virtual authenticator](https://chromedevtools.github.io/devtools-protocol/tot/WebAuthn/)
(`WebAuthn.enable` and `WebAuthn.addVirtualAuthenticator`: `ctap2`, `internal`
transport, resident keys, user verification on, automatic presence simulation).
A person signs in as the `gh` account (verified here as `rock-ai-bot`) in that
headed browser and registers a passkey in GitHub's security settings. Read the
credential with `WebAuthn.getCredentials`, close the browser, and in a fresh
browser with no cookies add it back with `WebAuthn.addCredential`. GitHub's
"Sign in with a passkey" then completes without a 2FA code.

A local check, with no GitHub account, showed the mechanism is sound. It
registered a resident credential on a locally intercepted HTTPS origin, exported
it, imported it into a new browser, and requested an assertion with a new
challenge and no allowed-credential list. The signature verified against the
original public key, with the right challenge, origin, relying-party hash,
user-presence and user-verification flags, and an increased counter. This proves
credential persistence, not GitHub's acceptance of it.

**Live result (2026-10-26).** A person enrolled as `rock-ai-bot` using a throwaway
script: GitHub accepted the virtual authenticator's registration and the credential was saved.
A replay then started a fresh browser with only that credential, and the
passkey button signed in successfully. This is one manual trial. It does not
show that GitHub keeps accepting the credential over time, that the counter
stays in step across many replays, or that GitHub never asks for extra
verification (new location, bot checks). A first attempt that closed the
browser before saving left an orphaned passkey on the account, so remove any
stray "OpenEngine benchmark spike" entries from the bot's security settings
after a failed enrollment.

**Credential decision and fallback.** Store the full CDP credential
(including private key, user handle and counter) at
`.engine/benchmark-github-passkey.json`, covered by the root `.gitignore`, with
mode `0600`. Record no screenshots, traces, cookies or storage state.
The credential is a virtual one registered inside the automated browser, not an
existing OS or hardware passkey. Persist the updated counter after each replay;
serialize reuse of this credential. CI would read it from a dedicated benchmark
environment secret `BENCHMARK_GITHUB_PASSKEY_JSON`, materialized with the same
permissions, and needs an explicit process for persisting updates. Until that
exists, keep browser trials local. App keys and tokens also remain outside
committed artifacts.

If GitHub stops accepting the virtual passkey (the one manual trial passed), keep the baseline on token auth and
repository webhooks. A browser-specific follow-up can use the bot password plus
harness-computed TOTP from a separately stored seed (CI environment secrets or
gitignored local files); TOTP is a proposed fallback, not tested here. Bot checks
or recovery prompts still require an operator. Do not silently fall back to a
personal account.

### Setup findings (#1143)

The whole journey runs in a Docker container, a clean Linux machine, and not on
the developer's computer. Setup written against the host first showed why:
`oe daemon` registers its background service under one fixed name per user (the
launchd label `sh.openengine.engine`, or the systemd unit `openengine.service`),
so installing the benchmark's OpenEngine would take over a developer's own;
`oe`'s `local` backend is always port 4364, so a daemon on any other port is
invisible to `oe agent` and `oe graph`, which would quietly act on whatever else
is on 4364; and the installer writes into the home folder. In the container the
daemon uses its process backend, 4364 is free, and teardown is removing the
container, which also stops the tunnel.

- The image installs Linux `claude`, `codex`, `gh` and `cloudflared`; the
  host's are macOS programs a Linux container cannot run. It holds nothing of
  OpenEngine, which the journey clones and installs from `main`, timed apart
  from the run and recorded with its SHA.
- Logins are mounted read-only and copied to where a person's own would be: the
  `gh` config directory, Claude's `.credentials.json` and `.claude.json`, and
  Codex's `auth.json`. A token the container refreshes never changes the
  host's. `[claude] config_dir` and `[github] gh_config_dir` exist, but the
  defaults suffice once the logins are where a new user's would be.
- `oe agents` lists runners but not whether they are signed in, and `oe agent
  signin` waits for a person. `oe doctor` runs `claude auth status` and reports
  it without prompting; it does not check Codex, so the journey runs `codex
  login status` as well. Both fail the run early with a message saying what to
  sign in to. `oe doctor` prints the Claude account's email, so that row is
  masked in the screenshot.
- The tunnel's startup checks (`TUNNEL_NO_PRECHECKS=true`) are skipped, or their
  output scrolls the public address off a 40-row terminal within a second.
- `docker build` asks the registry about its base image even when nothing
  changed, which needs Docker's credentials helper, and on a Mac over SSH that
  needs an unlocked keychain (`security unlock-keychain`). The image is built
  only when its Dockerfile changes, so later runs need neither. The same locked
  keychain makes `claude` and `codex` hang when launched from such a session.

### Decision: the real run feeds the committed guide

The real run's test2doc output and screenshots are committed as a guide, and the
diff is noisy on purpose. Each release's guide is a real record of what
OpenEngine did that time.

- The benchmark guide is its own page and doc category, separate from the
  existing guides. Those rely on byte-for-byte identical output
  (`e2e/README.md`), so one noisy page must not churn them or trip any check
  that the committed guides are up to date.
- It is regenerated only by a release-time benchmark run, never by `npm run doc`
  on a developer machine. The benchmark spec carries a `@benchmark` Playwright
  tag. `npm run doc` already excludes tests with `--grep-invert @skip-docs`, so
  it adds `@benchmark` to that exclusion, and `npm run oe-benchmark` runs only
  the tests with that tag (`npm test` skips it). Its page is written to
  `site/docs/benchmark/`, apart from the guides. The bench repository is always
  named `oe-bench`: a setup project deletes any existing one before each run, so
  a failed run leaves its repo, folder and tunnel in place to inspect.
- The release PR carries the diff, so a person reads it as part of the release
  review. Run IDs, ports and timestamps still use the terminal page's stable
  stand-ins, which cuts the noise to what the model actually did.

## Guides

The journey is also documentation. The benchmark's output is committed as the
separate, noisy benchmark guide described under Test harness. The guide is
generated Docusaurus markdown in the docsite (`site/`), with a section per step
of the journey that shows how to build a project with OpenEngine, from seeding
the issues to the deployed site.

## Assumptions to check

Check these early and change the plan if any turn out wrong:

- The harness can approve a run's `human_review` step from the CLI. Human steps
  are answered in the web UI or interactively with `oe run watch RUN_ID`, so the
  harness may drive `oe run watch` in the xterm.js terminal.
- A loop with a short `--every` works a milestone cleanly: no overlapping runs
  on the same issue, and the shortest interval `oe loop add` accepts.
- Merging a `tdd-guide` PR closes its issue, so Pick moves on to the next one.
- The bot's saved Chromium virtual passkey keeps working across repeated fresh
  browser sign-ins (one manual replay passed; see #1137 findings).
- `tdd-guide` gives its PRs Conventional Commits titles, so the changelog works.
  If commitlint fails a PR on its title, `tdd-guide`'s CI retry sends it back to
  the implementer, which may fix code rather than the title.

## Order of work

1. File the "E2E benchmark" milestone, one issue per reviewable piece of work.
   Work it with the `tdd-guide` graph.
2. Spikes first: check the assumptions above, and run the Slack spike, so a
   failed assumption or a dropped Slack layer changes the plan before the
   harness is built.
3. Harness, following the run sequence: teardown, clone at a ref, OpenEngine
   setup (GitHub App, Cloudflare Tunnel, `tdd-guide` registration), repo
   creation and settings, seeding, merge loop, verifier, docs and release
   checks, committing the guide. Mask secrets in screenshots from the start. Check the verifier
   against a hand-written calculator first.
4. Write the seeded milestone and issues, with "Depends on" lines, and check
   they build the calculator in order.
5. Get one passing run with the fixed runners, with `tdd-guide` working the
   milestone.
6. Report, budget cap, then several trials to set the baseline.
7. Layer 2, then layer 3 if the Slack spike says it can be automated.
8. Decide the release gate once there is a baseline.
