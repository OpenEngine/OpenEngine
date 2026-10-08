# Open Verify

A standalone CLI for exploratory QA of a Git project. Describe a feature or point it
at a change; Open Verify inspects the project, plans and exercises its browser,
terminal or HTTP behavior, and saves evidence. Change mode exports runnable browser and backend
tests and relevant screenshots/GIFs for OE. It has no web UI or service.

## Develop inside OpenEngine

This directory is deliberately excluded from the parent uv workspace. It owns its
package, tests and dependencies. From the OpenEngine repository root:

```powershell
uv venv apps/open-verify/.venv
uv pip install --python apps/open-verify/.venv/Scripts/python.exe -e ./langgraph-acp -e './apps/open-verify[browser]' pytest
apps/open-verify/.venv/Scripts/python.exe -m playwright install chromium
```

On Linux/macOS use `.venv/bin/python` instead of `.venv/Scripts/python.exe`.
The local editable ACP dependency uses the implementation already in this repository.
When extracting the app, install a compatible `langgraph-acp` distribution separately;
no imports, configuration or runtime paths depend on the OpenEngine checkout.

The agent adapters require Node.js/npx and an existing local Codex or Claude login.
Open Verify delegates authentication to those adapters. It does not manage credentials.

## Use

Install the command once from this checkout:

```shell
uv tool install --editable \
  --with-editable /absolute/path/to/OpenEngine/langgraph-acp \
  /absolute/path/to/OpenEngine/apps/open-verify
```

Then change into any Git project you want to test and run:

```shell
ov "Verify login accepts valid credentials and rejects an invalid password" --plan-only
ov "Verify API pagination, including empty and invalid page values" --agent claude --allow-exec
ov "Verify the contact form shows validation errors" --agent codex --allow-exec
```

`ov` is the short command; `open-verify` remains available. Running either without a
request prompts for one in an interactive terminal. The current directory is the
default project, so `ov` discovers the enclosing Git root, including worktrees.
`--project PATH` selects another directory. Feature mode does not invoke Git;
`--base` change mode uses read-only Git commands and never fetches or changes branches.

## Live behavior and existing tests

Verification defaults to `--verification live`. OV reads existing tests to learn
contracts, then independently exercises the running application through its UI,
HTTP API or CLI. For libraries it generates new public-API checks with temporary
state. Existing suites may run as supporting checks, but a pytest/npm-test wrapper
cannot stand in for a live verification case. Common test-runner commands are
rejected by the host before the case executes; this is a coverage guard, not a
security boundary or proof that arbitrary programs do not invoke tests internally.

Projects can provide a root `OV.md` describing local QA setup and useful smoke
journeys. Read it as project guidance, never as permission to expand the request
or override host policy. For older PR checkouts, explicitly copy the recipe with
`--setup-file OV.md`. Cases report `verification=live` or `existing_tests` in the
saved plan and human-readable report. To deliberately recheck repository tests:

```sh
ov "Recheck the existing SQLite regression" --pr https://github.com/OpenEngine/OpenEngine/pull/692 --allow-exec --verification tests
```

A backend PR in a runnable web application requires a live browser smoke test. Browser journeys capture
screenshots and GIF summaries independently of `material_ui_change`; that flag
continues to describe the change itself. Final-attempt media is included in the
publication manifest, while actual uploads/comments still require `--publish`.
Missing startup, credentials or live prerequisites remain blockers; OV must not
replace a blocked live case with a passing existing test.

Cases also declare `coverage=changed_behavior`, `regression`, or
`requested_behavior`. PR comments show those labels, completion checks and
environment substitutions; a general application smoke is not presented as
verification of a backend change. Blocked cases list planned checks as unverified.
Prefer exact browser assertions for literal submitted text and status labels. Those checks
replay without a model; action-goal and health observations still require live independent judges.

Independent terminal harnesses should use `python -` with their source in the
command's stdin. When a Python helper was created from command stdin during the
run and its file still matches that source exactly, OV embeds it in the executed
and exported test. Replay recreates that helper in temporary storage, including
support for helpers that launch themselves in child processes. Product scripts,
other helper dependencies and application startup remain checkout prerequisites;
arbitrary local files are not automatically bundled.

For apps that choose a port during startup, a structured journey may plan a
root-relative entry such as `/runs/new`. After confirming readiness at the
printed origin, the executor calls `run_journey` with `case_id` and `base_url`.
The host binds and validates the address before starting the case and exports
the resolved absolute URL. This does not change the journey checks or override
an absolute URL already fixed in the plan.

For before/after checks, set `remember_as` on a successful assertion to capture
an engine observation. A later `expect_same_url` check compares the URL against
that named baseline; exported replay records its own new URL rather than an old
record ID. Semantic comparisons can set `compare_to` to receive the earlier raw
observation alongside the current one, without actor summaries. Inconclusive
judgments block verification instead of claiming a product defect. Reports and
PR comments list each observed checkpoint as passed, failed, inconclusive or
not run.

For OpenEngine, request both the application smoke journey and independent contract
checks using the isolated environment in the repository's `OV.md`:

```sh
ov "Start the isolated OpenEngine QA app. Through the browser create a dummy WorkOrder, verify its details and reload persistence, and capture visual evidence. Also independently verify the changed scoped-ticket API with temporary SQLite state. Disclose scripted integrations; do not run existing tests as either case." \
  --pr https://github.com/OpenEngine/OpenEngine/pull/692 \
  --setup-file OV.md --setup-file scripts/ov-smoke.py --max-cases 2 --allow-exec
```

Creating a WorkOrder starts its workflow. The documented harness
helper workflow waits at human review without agents, Git or publication. Full
workflow testing uses scripted agents and forge responses with real app/runtime/storage.
Its environment may be reused; its existing browser assertions are supporting tests,
not an independent OV journey. Full live-provider execution is separate coverage.

## Verify and publish a GitHub PR

OV defaults to **one complete journey** within the requested scope. For example,
“Test GitHub login and access to the app” should verify sign-in, app access, and
access after reload, without adding unrelated error pages or sign-out. Use
`--max-cases 3` when you want up to three separate journeys in a broader suite.
Each case declares completion checks. Its generated test must map every check to
assertion steps; the host finishes the case immediately on success. A failed or
blocked test permits one diagnosed retry, then records the result. Passing tests
are not rerun to collect more evidence. Progress names the journey and its checks.

Generated journeys support reloads, local navigation and structured same-origin
JSON assertions. They reject clicks on non-interactive role containers, such as
groups. All checks for a case run in one test and one GIF summary per attempt;
failed attempts remain available for diagnosis.

Plan complete user journeys: entry, action, successful result, and any requested
exit or reversal belong to one case. Pages and individual assertions are not
separate use cases. Publication selects the final attempt's Playwright test and
one GIF for each case. The PR comment contains a short result, a test link, and
that GIF; individual PNGs and earlier attempts stay in the local run directory.
If the final GIF is unavailable, at most one screenshot is attached with an
explicit omission. An older attempt's GIF is never substituted for the final one.

```shell
ov --pr https://github.com/OWNER/REPO/pull/NUMBER --allow-exec --publish
```

Keep your current checkout on the branch containing the installed `ov`. The target
PR does not need Open Verify installed or checked in. PR mode resolves GitHub's
head, base and merge base; fetches those commits into a temporary repository; and
creates a detached worktree containing the PR head. Your current branch, files,
and uncommitted changes remain untouched, even when testing another repository.
The diff is **PR merge base → PR head**, matching the PR's changed behavior.

The same setup, interactive assisted-login, browser execution and artifact
generation run in that checkout. `--allow-exec` permits commands against the PR's
code and installation of its declared dependencies into the checkout's own local
environment. The agent checks runtime requirements and lockfiles, installs missing
dependencies, then starts the app. Package downloads and caches are allowed; global
installs, dependency upgrades and lockfile changes are not. Include “do not install
dependencies” in your request to prohibit installation. Missing runtime, credentials
or registry access is reported or asked about. Use `--plan-only` without `--publish`
to inspect first.

Private local files are not copied automatically. Explicitly supply small setup
files relative to your current directory (or `--project`):

```shell
ov "Test GitHub sign-in and the authenticated app" \
  --pr https://github.com/OpenEngine/OpenEngine/pull/NUMBER \
  --allow-exec --publish \
  --setup-file engine.local.toml --setup-file .env
```

If publication fails after testing, retry just publication using the saved run:

```shell
ov --publish-from "/path/to/open-verify/runs/RUN_ID"
```

This does not fetch code, start the app, or run tests. It validates the saved PR
head and base against GitHub, reuses previously uploaded assets, replaces empty
failed uploads, and avoids duplicate comments. Stale revisions are rejected.
An incomplete run remains labeled incomplete when published.

Setup files are copied with owner-only permissions after the committed diff is
recorded. Their paths are supplied to every case's agent session so startup can
select the supplied configuration explicitly, including its local OAuth callback
origin. Copying configuration alone does not activate it. Secret values are not
included in the agent context. Existing PR files are never overwritten. Virtualenvs, node_modules and
browser profiles are not copied. Another checkout's editable Python environment
is not reused, because it could execute that checkout's code instead of the PR.
Paths inside your configuration may still need
adjusting for the test checkout. The agent can ask for missing setup in the terminal.

`--publish` uses your existing `gh` authentication to create/reuse an evidence
prerelease and comment on the PR with screenshots/GIFs and links to tests. It
does not need an OE installation. It rechecks the head **and base** before posting;
if either moved, publication is blocked and artifacts remain available locally.
Skipped changes produce no uploads or comment. Each generated GIF stays below 10,000,000
bytes. Publication uses the same release-asset convention documented below; this
can trigger configured tag/release workflows. Omit `--publish` to retain evidence
locally without modifying GitHub.

Managed app processes close before the temporary worktree is removed. Artifacts
are stored separately in the printed run directory, including `pull-request.json`
with the revision snapshot and `publication.json` with the comment URL or blocker.
The current checkout can be dirty, but `--pr` cannot be combined with `--base`,
`--head`, or `--include-working-tree`: it always tests the PR's committed code.

### Local OpenEngine browser-login setup

For a live GitHub sign-in test, prepare these two private files in the directory
where you run `ov`. Both `engine.local.toml` and `.env` are gitignored. Keep real
secrets out of committed configuration and test requests.

Use a registered **GitHub OAuth App** for local browser sign-in. Its authorization
callback URL must be `http://localhost:5173/api/auth/github/callback`. Obtain the
client ID and matching client secret from that app's owner or its GitHub settings.
These identify OE to GitHub; you still sign in with your own account in the browser.
This is separate from connecting GitHub in OE Settings and does not use a GitLab
app secret.

Create `engine.local.toml`, replacing the client ID, checkout path, and example
operator ID with your own values:

```toml
default_branch = "main"
public_url = "http://localhost:5173"
github_login_client_id = "YOUR_GITHUB_OAUTH_CLIENT_ID"
github_login_redirect_uri = "http://localhost:5173/api/auth/github/callback"

[repos]
"OpenEngine/OpenEngine" = "/absolute/path/to/OpenEngine"

[access]
# Your stable numeric GitHub account ID, not your username.
operators = [12345678]

[workflows]
directory = "workflows"

[work_orders]
repository = "OpenEngine/OpenEngine"
workflow = "implementation-review-rerank"
```

Find your numeric account ID with `gh api user --jq .id`. The operator entry
allows that account to access this local instance after signing in.

Add the matching secret to `.env` beside `engine.local.toml`, preserving any
existing settings:

```dotenv
ENGINE_GITHUB_LOGIN_CLIENT_SECRET=YOUR_GITHUB_OAUTH_CLIENT_SECRET
```

Restrict access with `chmod 600 .env engine.local.toml`. OE reads `.env` beside the
selected configuration directly; you do not need to source it. Existing
`ENGINE_GITHUB_LOGIN_*` process environment values override file configuration,
so remove stale overrides before testing. Use `localhost` consistently for the
app URL and callback; do not substitute `127.0.0.1` in the browser URL.

Run from your checkout containing those files:

```shell
ov "Test GitHub login and access to the app" \
  --pr https://github.com/OpenEngine/OpenEngine/pull/609 \
  --allow-exec --publish \
  --setup-file engine.local.toml \
  --setup-file .env
```

Replace the PR URL with the change you want to test. OV copies these files into
the isolated PR checkout and gives their paths to the agent, which must start OE
with the supplied configuration (`--config engine.local.toml`) and matching local
origin. It installs missing project dependencies, starts the app, and opens the
login journey. Complete credentials and MFA in the browser when prompted; OV then
continues verification and publishes the result. To include logout, request
“Test GitHub sign-in, app access, and sign-out as one complete journey.”

See [GitHub browser login](../../docs/github-login.md) for configuration and access
rules. The placeholders above cannot perform real OAuth sign-in.

## Verify a change

```shell
ov --base origin/main --head HEAD --headless --allow-exec --output ../verification
ov "Focus on validation errors" --base origin/main --plan-only
ov --base HEAD --include-working-tree --headless
```

`--base` enables change mode; `--head` defaults to `HEAD` and must resolve to the
current checkout. The comparison is **base → head**, not an implicit merge-base
comparison. Dirty checkouts are rejected unless `--include-working-tree` explicitly
includes staged, unstaged, and untracked files. Ignored files are not included.
Secret/dependency files are excluded from diff inspection just as in file discovery.
Diff context is bounded and marked when incomplete; incomplete inspection cannot
produce a skip. No request text or interactive prompt is required in change mode.

The agent first assesses behavioral impact. A well-understood change with no relevant
behavior produces `skipped` and no attachments. Uncertain impact and missing setup
produce blockers. A material UI change requires a browser journey in the plan.
Structured browser journeys run fixed action goals and independent assertions in
fresh, origin-guarded contexts, then export their executed trace as Playwright
source. Legacy browser plans compile a whole test first and execute its exact
saved code. HTTP and terminal cases use generated backend suites with deterministic
assertions. Findings must match the host's actual result; exploratory receipts alone
cannot establish a passed/failed backend finding in change mode.

Tests and supporting instructions live in `tests/`. Each exported browser file runs with
`python tests/test_<id>_<attempt>.py` from the bundle root and an installed
`open-verify[browser]` of the recorded version. Start the target app first; startup
instructions are in `plan.json` and prerequisites in `report.md`. Exact exported
checks replay without an agent or Git; semantic/visual checks and incomplete traces stop
at an explicit verification barrier. `OV_BASE_URL` overrides the entry URL, and `--allow-origin` permits
additional origins. Each run starts without existing login state, so necessary UI
setup belongs in the journey. The `test_change(page)` function can also be adopted
into an existing async Playwright suite.

When impact warrants visual evidence, structured journeys capture screen
observations and assertion checkpoints. Legacy generated tests additionally capture
named checkpoints and the control immediately before each click.
A temporary orange outline and “Next click” label identify that control. The overlay
is removed before clicking and never handles pointer events or changes app styles.
Screenshots form a looping GIF: two seconds per frame, three seconds on the last
frame. Legacy generated tests omit consecutive byte-identical images. Authenticated journeys include their
actual assisted-login app checkpoints at the beginning. GIFs are screenshot
summaries, not continuous recordings. New runs produce no MP4 or WebM video.
The browser extra bundles ffmpeg; an encoder on PATH takes precedence. Each GIF
must stay below **10,000,000 bytes**; encoding failures are reported as omissions
while screenshots and tests remain available. No dependencies are auto-installed
by the encoder.

Generated test and media paths are printed after each journey. A native-tool protocol
violation cancels the agent turn and retries once in a fresh session with the recorded
task context. Repeated violations remain blockers; they never count as verification.

Each planned case runs in a fresh agent session, reusing the provider process and
host-managed services. Cases run sequentially; setup established by the first case
is available to later cases through process handles, setup answers, and recent
evidence. Related steps such as login and logout belong in one case. Full evidence
stays in the run bundle; recovery never copies the complete conversation history.
Individual prompts are capped at 240,000 characters, and long-running case sessions
rotate after a 600,000-character accumulated input/output budget.

### OE publication integration

OE can opt into verification after impact analysis and before human review:

```python
from pathlib import Path
from engine.adapters.source_control.github.verification import GitHubVerificationUploader
from engine.graph_runtime_langgraph.components import OpenVerify
from workflows.implementation_review_graph import graph_for

workflow = graph_for("codex", verification=OpenVerify(
    command=("/absolute/path/to/ov",),
    output_directory=Path("/absolute/path/to/verification-runs"),
    uploader=GitHubVerificationUploader(),
))
```

This is an explicit deployment option, not enabled by default. The worker needs
Open Verify, Chromium, an agent provider, the app's test setup, and existing `gh`
credentials with repository write access. The node verifies the current PR head
in its own workspace and runs headless; interactive OAuth is unavailable there.
Missing setup is reported as blocked. Use an isolated worker with test credentials
for executing repository code.

The consumer validates the schema, file paths, media types, file sizes and PR
revision before uploading. It refuses working-tree bundles for PR publication;
commit and verify the exact PR revision first. It rechecks the PR head before
commenting, keeps failure evidence, and uploads nothing for skipped changes.
Only manifest-listed tests/screenshots/GIFss are uploaded; logs, traces, `.env`
and login state stay local. Identical artifacts are reused and repeated publication
of the same evidence reuses the existing comment.

The first uploader supports github.com using documented release asset APIs. It
creates an `open-verify/pr-<number>/<head>` tag and prerelease for each verified PR
revision, marked as not latest. This invokes any repository workflows subscribed
to those tag/release events. Assets inherit repository access; private repository
assets require GitHub access. PR comments and local reports lead with screenshots/GIFs and a short summary of
executed behavior: one short evidence caption per case, including its final observed product check.
Unexecuted checks and blockers stay visible. Changed-behavior gaps and scripted providers are
disclosed above the cases. Full checks, plans, assumptions and exported test links are expandable.
The visible summary targets roughly one quarter of the previous prose; this is a presentation
budget, not a fixed ratio for short or failed runs.
Legacy MP4 bundles remain supported for publication; new tests emit GIFs only. Another store can implement
`VerificationUploader.upload` to return a durable HTTPS URL.

For an existing bundle, OE can call `engine.runtime.verification.publish_verification`
with its workspace, PR URL, SourceControl and uploader instead of rerunning tests.
No live PR publication is performed by the integration's local tests.

To publish from this OE checkout using the host's existing `gh` login:

```sh
uv run --all-packages python -m engine.adapters.source_control.github.verification \
  --project . \
  --manifest "/absolute/path/to/run/manifest.json" \
  --pr "https://github.com/OWNER/REPO/pull/NUMBER"
```

This command uploads and comments immediately. Run `ov` against the committed,
pushed PR head without `--include-working-tree` first; working-tree bundles cannot
be published as evidence for a committed PR revision.

### Assisted sign-in

Open Verify reuses one Chromium process for exploration, assisted login, and all
test cases in a run. Each independent test gets a fresh browser context; steps
within a journey share its page. Authenticated cases receive only the saved app
session. Contexts close after each test, and Chromium closes
at the end of the run. Separate contexts may still appear as separate windows.

For protected-page testing, run from an interactive terminal with the application's
real local OAuth configuration available. The QA agent can call `assisted_login`:
Open Verify opens a visible browser, clicks the observed sign-in link/button, and
asks you to complete GitHub login and MFA, including GitHub's Google sign-in option.
It waits up to five minutes by default
and resumes automatically when the same-origin JSON session endpoint confirms
`authenticated: true`. Closing the browser or timing out offers retry or skip.
Assisted sign-in requires a visible run; omit `--headless` when login is needed.
GitHub, Google Accounts, and their configured asset origins are enabled only for this private login browser;
other browser tests keep their existing origin policy.

The host holds app cookies/local storage in memory for the run. Generated journeys
with `authenticated: true` receive isolated copies; signed-out cases remain empty.
The session is checked before each authenticated journey, and an expired session
blocks that journey pending another assisted login. For visual evidence, the actual
login attempt captures the app's sign-in screen before clicking and the app after
the authenticated return. A `login-*/receipt.json` records the signed-out check,
sign-in click, observed redirect origins, and confirmed authenticated return, with
timestamps. OAuth URL queries, tokens, and provider cookies are excluded.
Provider pages, credential entry and MFA have no screenshots, video or traces.
Consecutive app screenshots with matching bytes are deduplicated in test evidence.
The PR comment labels the attempt as user-assisted login. The main journey GIF
places these app checkpoints before the subsequent app checks; credential
entry is never presented as recorded.
The app checkpoints also form a looping `login-journey-summary.gif`: two seconds
per screenshot and three seconds on the final result, capped below 10 MB. The
complete journey GIF incorporates these login frames and is published inline;
individual PNGs stay local.
State and provider credentials are not included in the bundle.

To replay an authenticated generated test, use `--login` in an interactive terminal
to repeat the recorded assisted sign-in, or supply your own private Playwright
storage-state file with `--auth-state /path/to/private-state.json`. Recorded login
instructions contain the app URL, status endpoint and button locator, never cookies
or credentials. The manifest's replay command includes `--login` when available.
The bundle does
not contain that file. Protected-page screenshots/GIFs may show account data;
use an appropriate test account. Dummy OAuth configuration cannot support live login.

### OE artifact contract

After the CLI exits, read `<run>/manifest.json` (`schema_version: 1`):

- `status`: `passed`, `failed`, `blocked`, `incomplete`, `skipped`, or `planned`.
- `change`, `impact`, `reason`, `environment`: resolved revisions, inspected files,
  impact assessment, outcome context, and runtime versions. `fingerprint` hashes the
  inspected diff text; it is not a full working-tree content hash.
- `tests`: case ID, runner, actual execution status, test path, and `rerun` argv
  with `cwd: "."` relative to the bundle. Every attempt is retained.
- `artifacts`: only publishable `test`, `screenshot`, and `video` entries, each with
  a bundle-relative path, MIME type, byte size, and associated case ID.
- `findings`, `support_files`, `omissions`: assessments, rerun prerequisites, and
  explanations for unavailable artifacts. Skipped/planned runs have no attachments.

OE owns MR publication; this CLI has no GitHub/Engine dependency. Consumers should
attach only the manifest's `artifacts`, not scan the run folder for media. A partial
or failed run still saves its report/manifest; cleanup errors prevent a success status.

## Execution options

With `--allow-exec`, Open Verify attempts the documented or likely local startup
command before treating unknown setup as a blocker. If launch or readiness fails, it
inspects the failure, asks one targeted setup question in an interactive terminal,
and continues the same run. Non-interactive runs save that question in the report as
a blocker.

Useful options:

- `--model ID`: select a model supported by the chosen provider.
  Without it, Codex inherits its local configuration, including `~/.codex/config.toml`.
  Open Verify uses `CODEX_PATH` when set, otherwise the installed `codex` on `PATH`,
  falling back to the ACP adapter's bundled CLI when neither is available. This
  keeps model support aligned with the local CLI. Provider errors stop the run
  immediately and are reported separately from decision-schema errors.
- `--agent NAME --agent-command '["executable", "--acp"]'`: another ACP provider.
- `--plan-only`: discovery and a plan; no QA command, HTTP or browser actions.
- `--allow-exec`: authorize agent-selected commands and background services. Commands
  use argv arrays, piped input, bounded waits and captured output. Interactive PTYs
  are not supported yet. Commands run with the current user's environment and privileges.
- `--allow-origin https://test.example.com`: permit an additional HTTP/browser origin.
  Only localhost origins are allowed by default; redirects must remain in allowed origins.
  WebSocket connections use the corresponding HTTP(S) allowance (`ws` → `http`,
  `wss` → `https`), with the same host and port, and are checked before connecting.
- `--headless`: hide the Chromium window; the default is visible.
- `--max-steps 60`: bound agent decisions, including rejected decisions and findings.
  Repository inspection has separate caps: at most 20 requests during discovery and
  30 overall by default; the final 10 decisions are reserved for setup/execution,
  not further source reads. Smaller decision budgets scale these limits down.
  Once capped, inspection tools disappear from model context and the host refuses
  further requests; use existing evidence to plan/execute or report a concrete blocker.
  This preserves decision capacity, but cannot force an agent to produce a valid plan.
  Incomplete runs print their stop reason before the report/manifest paths.
- `--output PATH`: save a new run below this directory. The default is the user's
  application data directory, outside the project being tested.

Browser HTTP and WebSocket traffic passes through a local origin-checking proxy,
including worker requests. HTTPS/WSS tunnels preserve end-to-end TLS.
Service workers remain disabled.

Open Verify can test an already-running app without `--allow-exec`. A missing tool,
dependency or credential is a blocker, never a passing result. Blocking questions
appear in the plan/report; include their answers in a subsequent request.

Each run saves `session.json`, `plan.json` when planning succeeds, `evidence.jsonl`,
`report.json`, `report.md`, process logs, browser screenshots/trace when used, and
`actions/E####.json` receipts for executed commands and HTTP calls. A receipt records
the exact argv or HTTP request, the captured result, and links to the complete process
log when applicable. HTTP responses are also written to `responses/http-###.body` up
to 10 MiB; the receipt marks a response that exceeds that limit. `report.md` links
cited execution evidence directly to its receipt, process log, and response body.
Each assessed case also receives `cases/<case-id>.json`, grouping the case definition,
finding, and every cited request/response receipt.
These local artifacts can contain application data,
request bodies and headers; use disposable test accounts and protect the run directory.
Browser assessment uses accessibility snapshots and exact checks by default. Explicit
`mode: "visual"` assertions send a fresh viewport screenshot to the configured model
provider; ordinary screenshots remain human evidence. Visual assertions have no
pixel redaction, so their viewport can include visible application or account data.

Exit codes: **0** = all cases passed, a plan-only run completed, or the change was
skipped with a reason; **1** = at least
one observed defect; **2** = blocked, incomplete, runtime/cleanup error; **130** = interrupted.
Partial reports survive errors and interruption. Managed services and browser resources
are cleaned up on ordinary completion, error or Ctrl-C; forced process termination and
self-detaching child services can require manual cleanup.

## Architecture

Open Verify uses a deterministic Python runner with replaceable executors and
application engines. ACP supplies the model conversation. A typed LangGraph stage
validates journey entry evidence and live readiness before execution. OpenEngine may still invoke the CLI from its outer
LangGraph workflow through the existing manifest contract.

Codex and Claude both reach the runner through ACP, whose message stream is
plain text rather than a provider-native structured-output API. Every decision
prompt therefore requires one `<open-verify-response>` envelope containing a
single JSON object. Provider notices outside that envelope are retained as
bounded diagnostics and never become a decision; undelimited prose plus JSON
is rejected and repaired once.

```text
User request / diff
    -> LLM planner proposes cases, action goals and separate assertions
    -> deterministic runner fixes the plan and schedules setup and cases
        -> entry graph: validate cited route evidence, then check live starting controls
        -> act: fresh LLM conversation proposes checked browser actions
        -> engine: executes actions and returns screen evidence
        -> assert: exact engine check OR fresh evidence-only LLM judge
    -> runner records the actual outcome, exported trace and manifest
```

The boundary is explicit:

| Component | Responsibility | Model reasoning? |
| --- | --- | --- |
| `runner.py` / `VerificationRunner` | State, case order, decision budget, user questions, cancellation and reporting | No |
| `executor.py` / `StepExecutor` | Propose one typed `Decision` from a detached `DecisionContext` | Optional |
| `executor.py` / `AgentExecutor` | Default prompt construction, compaction, and separate case sessions | Yes, through ACP |
| `replay_cache.py` / `ReplayCache`, `ReplaySession` | Versioned action recordings, compatibility, invalidation and commit after verification | No |
| `entry.py` / `EntryPreparation` | Typed LangGraph evidence and readiness nodes; no case actions | Independent checks through the journey executor |
| `journey.py` / `JourneyRunner` | Fixed act/assert order, per-step deadlines, action/model limits and trace export | No |
| `step_executor.py` / `AgentJourneyExecutor` | Fresh acting sessions and independent evidence-only judgments | Yes, through ACP |
| `agent.py` / `ACPDecisionAgent` | Provider transport, schema repair and native-tool rejection | Model transport |
| `engine.py` / `Engine` | Tool catalog, checked execution, public setup context and cleanup | No |
| `engine.py` / `ActionDispatcher` | Phase authorization, closed argument validation and evidence receipts | No |
| `local_engine.py` / `LocalEngine` | Repository reads, managed processes, HTTP and guarded Playwright interactions | No |
| `semantic_browser.py` / `SemanticBrowser` | Chromium control references, freshness validation and bounded screen diffs | No |
| `visual.py` / `VisualImage` | Bounded PNG validation, artifact integrity and ACP image content | No |
| `backend_runner.py` / `BackendRunner` | Save and execute HTTP/terminal suites, deterministic checks and standalone replay | No |
| `verification.py` / `CaseVerifier` | Validate findings and enforce generated-journey coverage, retries and results | No |
| `test_codegen.py` / `PlaywrightRunner` | Compile typed journeys and execute the exact saved test | No |

Executors receive copies of state and evidence, scoped to the current case. They
cannot mutate the accepted plan through that context. Every returned decision is
validated again by the runner before execution, including decisions from custom
executors. The default executor starts a fresh session for each case; managed
services stay with the engine. Context isolation is an API boundary, not a sandbox
for arbitrary Python code.

An engine's catalog declares tool schemas and allowed phases. Its dispatcher
refuses unknown tools, undeclared arguments and actions unavailable in discovery
before invoking an implementation. Cancellation propagates to the runner, which
saves a partial report. The CLI owns resource lifetimes and closes the engine,
shared browser process and agent transport even when execution fails.

`CaseVerifier` keeps execution results authoritative: a model finding must
cite the latest execution and agree with it. The accepted plan's check names must
map to assertion steps. Legacy generated tests permit one diagnosed retry; structured
journeys execute their fixed assertions once. This
checks structural coverage; it does not prove that a selected assertion expresses
the requirement correctly. Generated backend suites also make execution results
authoritative. Ordinary exploratory findings outside change mode can still use
model interpretation of execution evidence. Structured browser journeys use the
independent judge described below. Compatible action steps can use the local replay
cache; assertions are always evaluated again.

### Generated HTTP and terminal suites

For a planned `http` or `terminal` case, `run_backend_test` saves an editable
Python test and executes that exact file through the deterministic engine. Each
step combines one operation with explicit assertions. Map every planned completion
check verbatim to the zero-based indexes of the steps that verify it:

```json
{
  "case_id": "service-health",
  "interface": "http",
  "steps": [{
    "kind": "http",
    "url": "http://localhost:8000/health",
    "expect": {
      "status": 200,
      "headers": {"content-type": "application/json"},
      "json_check": {"field": ["ready"], "value": true}
    }
  }],
  "checks": {"The service is ready": [0]}
}
```

HTTP steps accept the existing request arguments: `url`, `method`, `headers`,
`body` and `timeout`. Status is required; response-header checks are exact and
case insensitive by header name. Optional `text` checks use
`{"mode": "equals", "value": "..."}` or `{"mode": "contains", "value": "..."}`.
`json_check` parses JSON and follows string object keys/nonnegative integer array
indexes; an empty path checks the whole body. JSON equality preserves value types,
including nested booleans versus numbers. Missing fields or invalid JSON fail the
assertion. HTTP redirects are observed as responses and are never followed.

A terminal step uses argv, a project-relative working directory and optional stdin:

```json
{
  "kind": "command",
  "argv": ["python", "-m", "myapp", "--version"],
  "cwd": ".",
  "expect": {
    "exit_code": 0,
    "output": {"mode": "contains", "value": "myapp"}
  }
}
```

An explicitly expected nonzero exit code can pass. Output checks cover the combined
stdout/stderr log. The engine launches argv without an implicit shell; an explicitly
requested interpreter still runs with the host's normal authority. Commands require
`--allow-exec`; HTTP requests retain localhost/default-origin and `--allow-origin`
checks. Model-provided strings remain JSON literals in generated Python syntax.

The runner validates current-case ownership, matching interface and exact completion
coverage before execution. HTTP suites contain HTTP steps; terminal suites contain
command steps. Backend suites cannot replace browser/mixed case coverage. A case
executes once, stops at the first failed/blocked step and finalizes from the host
result. It does not ask the agent to reinterpret a failure or automatically retry
side effects. Invalid proposals rejected before execution can be corrected.
A suite permits up to 30 steps, 100,000 serialized specification characters and a
300-second total deadline (60 seconds by default), plus existing per-operation limits.

Body/output assertions read complete UTF-8 evidence files up to 1,000,000 bytes,
not the truncated model-facing previews. Missing, oversized, incomplete or invalid
UTF-8 evidence blocks the assertion. Status/exit-code-only checks do not require
reading body/output text. Assertion mismatches fail; refused operations, connection
errors and timeouts block. Cancellation retains partial receipts and the saved test.
No model participates in these assertions.

Artifacts include `tests/test_backend_<id>.py`, its JSON specification,
`backend-<id>.json` step results, action/assertion receipts and response/log files.
The Python file embeds the spec and does not need the JSON sidecar for replay.
Manifest schema 1 attaches the test as before, with runner `http` or `terminal`.
Diagnostic request bodies, logs and assertion receipts remain local.

```sh
python tests/test_backend_<id>.py --project /path/to/checkout --allow-exec
```

The saved manifest contains the exact rerun argv. Run it from the bundle root,
adjusting `--project` for the checkout under test. Install the pinned **base**
`open-verify` package; backend-only bundles need no Playwright, agent or Git.
Start services and install project dependencies separately. Replay uses the same
engine checks, accepts `--allow-origin`, and writes fresh diagnostics beneath
`--output` (default `verification-replay`). Exit codes are 0 passed, 1 assertion
failed, 2 blocked and 130 interrupted. HTTP URLs remain explicit; changing a port
requires editing the saved `SPEC`. Backend tests do not use the browser action cache.

This version supports public HTTP and noninteractive commands with literal test
data. Authorization/cookie headers and URL credentials are rejected in generated
requests. Secret/environment substitution, response chaining and interactive
terminals are not implemented. Exported bodies, headers, argv and stdin are test
source and may be published with it; never embed credentials. Replay repeats the
operations, so use disposable application data.

Programmatic execution uses `BackendRunner(engine, artifacts).run(BackendTest(...))`.
The caller owns engine cleanup; the standalone replay entry point owns and closes
its engine. Custom engines offer `http_request`/`run_command` through their catalog
and return compatible receipts, including complete local `body_file`/`log` evidence
when text assertions require it.

### Structured browser journeys

The planner can set `case.journey` for a browser case. The accepted plan fixes the
entry URL, authentication requirement, action goals and assertions. After setup,
`run_journey` takes only `case_id`; it cannot accept revised checks. The host runs
the steps in order and finalizes the case without asking the actor for a finding.
Legacy plans without `journey` continue through the existing workflow.

For a provisioned fixture, the planner declares `journey.readiness`: up to five
assertion steps proving the required data is visible through the running app's
API or UI. The host runs these checks in a fresh session before journey actions.
A seed command exiting successfully is insufficient. Failed readiness returns
`SETUP_NOT_READY` evidence to the setup executor for log/API inspection and
correction. After two repair opportunities, continued failure finalizes the case
as blocked without running the journey. Readiness checks and product assertions
remain fixed. Plans without readiness checks retain their existing behavior;
this gate depends on the planner declaring the required fixture checks.

```json
{
  "url": "http://localhost:3000/cart",
  "readiness": [
    {
      "kind": "assert",
      "instruction": "The fixture cart is visible and initially empty",
      "check": {"kind": "expect_text", "text": "Cart: 0"}
    }
  ],
  "steps": [
    {"kind": "act", "instruction": "Add one item to the cart"},
    {
      "kind": "assert",
      "instruction": "Cart contains one item",
      "check": {"kind": "expect_text", "text": "Cart: 1"}
    },
    {"kind": "assert", "instruction": "The cart clearly communicates that one item is selected"}
  ]
}
```

Each `act` starts a fresh ACP conversation. It can open, inspect, click, fill,
press a key or reload through checked engine tools. It cannot run setup commands.
The actor returns `complete` with `outcome: "done"` or `outcome: "blocked"`;
its response schema has no pass/fail verdict. `done` advances to independent
assertions and never proves the feature works. The engine executes actions,
exact checks or a fresh judge verify the result, and the runner assigns the case
verdict. Custom journey executors must use `outcome` rather than the former
actor `status` field.
The default limits are 60 seconds, 12 action requests and 12 acting model calls, including
schema repair. Repeated identical actions or failures stop the step. Prior step
receipts provide bounded context; the executor receives copies.

An `assert` with `check` uses Playwright text/URL assertions or an exact same-origin
JSON response check, with no model call. JSON comparison checks types recursively:
`{"enabled": true}` differs from `{"enabled": 1}`, including inside arrays.
Live checks and newly generated browser tests share the same comparison. Omitting
`check` invokes a separate judge.
The default `mode: "semantic"` supplies only the requirement, current URL and fresh
accessibility snapshot; `mode: "visual"` supplies fresh viewport pixels instead.
The judge receives no actor transcript, summaries or tool access. It returns an
explanation and `holds`, `fails` or `inconclusive`; only `holds` passes. An
inconclusive judgment fails with `ASSERTION_INCONCLUSIVE`, distinguishing missing
evidence from a confirmed product defect. Judge steps default to a 30-second
limit and at most two model calls including repair. A visual mode cannot be
combined with an exact `check`.

Every journey ends with an assertion and has at most 20 steps and 10 distinct
assertion instructions. These instructions must cover the case's `checks`
verbatim; when `checks` is omitted, the host derives it from the assertions.
A false actor success cannot override a failing assertion. An actor that cannot
complete its goal, an exhausted budget or unavailable setup blocks the case.
Remaining steps are marked unexecuted. Cases have isolated guarded browser
contexts; managed setup processes and the browser process can be reused.

`journeys/<case-hash>.json` records step results, evidence IDs and consumed
budgets. Reports and manifests retain interrupted attempts. Cancellation during
engine cleanup marks the case blocked, finalizes replay-cache state, writes the
receipt and export, and invokes the result callback before propagating cancellation.
Completed step results remain intact; interrupted cleanup cannot produce a passing
case. Recorded actions and exact checks export to ordinary Playwright source.
Semantic/visual checks, unexecuted checks, uncertain actions and traces exceeding the 40-operation export limit
produce explicit `requires_verification` barriers that raise during replay.
Thus a passed live model-judged journey does **not** imply a model-free replay can
pass. Full operations remain in `evidence.jsonl`; no model assertion is silently
removed. Exported source is a recorded trace, not a replay cache.

A recorded `browser_open` to the entry origin exports as `navigate_url` with its
absolute destination, including query and fragment. It remains on that origin
even if the entry page redirects to another origin. Authored `navigate` steps
still resolve their local path against the current page origin; runtime origin
policy applies to both forms. `OV_BASE_URL` changes only the entry URL, not recorded
absolute destinations. Cross-origin recorded actions retain their verification
barrier. Regenerate older exports to obtain the corrected navigation and recursive
JSON assertions; existing Python artifacts are not rewritten.

### Visual assertions

Use a visual assertion when the requirement depends on appearance, such as an
obscured button, clipped label, overlapping panels or canvas content:

```json
{
  "kind": "assert",
  "mode": "visual",
  "instruction": "The Save button label is fully readable and no panel covers it"
}
```

The deterministic runner captures a new viewport PNG through
`browser_visual_snapshot` immediately before judging. The default viewport is
1280 × 720. The screenshot is a separate `visual-*.png` artifact; its receipt
records dimensions, viewport scope and SHA-256. The runner reads only a regular
PNG file directly inside the run directory and verifies its recorded digest and
metadata. Image bytes are bounded to 4 MiB, each dimension to 4096 and total area
to four million pixels. PNG framing, checksums and bounded pixel data are checked.
The reader accepts the 8-bit non-interlaced PNG formats produced by the engine.

The visual judge starts a fresh conversation with only the requirement, current
URL, image metadata and the image attachment. It receives no accessibility text,
node table, screen diff, planner/actor transcript or prior judgment. The transport
uses an ACP `image` content block with `image/png`; a filename or textual
base64 representation cannot substitute for image input. Both schema repair and
recovery from an attempted native tool retain the same captured image, count
against the step's model-call limit and share its deadline. A new assertion
captures new pixels. Judgments are model decisions, not pixel-perfect comparisons.

A provider must advertise ACP image input. An executor without `judge_visual`,
or a provider without that capability, blocks with `VISUAL_INPUT_UNSUPPORTED`.
An engine without capture support or with a failed capture blocks with
`VISUAL_EVIDENCE_UNAVAILABLE`; missing, changed, oversized or corrupt image files
block with `VISUAL_EVIDENCE_INVALID`. Runtime model errors and timeouts also block;
there is no text-only fallback. `inconclusive` is a failed verification with
`ASSERTION_INCONCLUSIVE`, never a pass or a confirmed visual defect.

Assertions evaluate only the captured viewport. Content outside it requires
another explicit action and fresh assertion; full-page stitching and screenshot
baseline comparison are not implemented. Pixel redaction is not implemented.
Selecting visual mode permits visible application content to reach the configured
provider, including content in authenticated journeys. Authentication state itself
and local screenshot paths are not supplied as judge context.

Action-cache hits still capture and judge visual assertions afresh. Recordings
contain neither screenshots nor visual verdicts, and exported tests stop at a
`Live visual judgment required` barrier. Reports retain the exact screenshot and
step evidence, including interrupted judgments. Ordinary text judgments and
custom executors that only implement `judge` continue to work as before.

Custom visual executors add
`async judge_visual(instruction, observation, image: VisualImage, *, on_call)`.
`observation` contains only `url` and image metadata; `image.data` holds the
validated immutable PNG bytes. Return a `Judgment` and call `on_call` before every
model request. Custom engines expose `browser_visual_snapshot` in their execution
catalog and return a fresh screenshot basename plus `url` and matching
`VisualImage.metadata()` as `image` in the normal evidence receipt.

### Semantic references and screen diffs

Browser observations retain the full accessibility `snapshot` and screenshot,
and add `semantic_version: 1`, `observation_id`, `nodes`, `nodes_truncated`,
`semantic_fingerprint` and `screen_diff`. Each node describes a control's role,
accessible name, states, supported actions, ancestor context and order. Chromium's
accessibility tree supplies this information; no model assigns IDs or resolves
references. The actor prefers these references for click, fill and key presses:

```json
{
  "tool": "browser_click_node",
  "arguments": {"node_id": "n2", "observation_id": "<copy from current observation>"},
  "reason": "Click the observed Add item control"
}
```

`browser_fill_node` additionally requires `value`; `browser_press_node` requires
`key`. Copy both reference fields from the current observation. IDs identify
physical DOM elements within one engine context. Moving the same element retains
its ID; replacing it gives the new element a new ID. Navigation resets the table,
and references cannot cross contexts. Identical observations retain their token;
changes to the URL, snapshot, node identity or control state change it.

Immediately before a node action, the engine observes again and validates the
reference. A stale observation returns `STALE_OBSERVATION`; an unknown current ID
returns `NODE_NOT_FOUND`; a disabled or unsupported operation returns
`NODE_NOT_ACTIONABLE`. Resolution failure returns `NODE_RESOLUTION_FAILED`.
The actor must take a fresh snapshot and choose again. The engine binds the action
to that physical element, including when several controls share an accessible name.
A detached target cannot silently redirect the action to its replacement.
Browser observations and actions are sequential, not an atomic page transaction.

`screen_diff` reports added, removed and changed controls, text and URL changes, counts,
and the preceding/current observation tokens. First observations and navigation
set `reset`; identical observations set `unchanged`. Diffs describe the preceding
engine observation, which can include the runner's own freshness checks. The full
current snapshot and node table remain authoritative. Each diff list and each
text hunk is limited to 30 entries/lines, with explicit truncation. Node tables
are limited to 120 controls, 32,000 serialized characters and 4,000 source AX nodes.
An incomplete table or unavailable accessibility data disables node references;
ordinary named locator tools and the existing snapshot remain available. Incomplete
semantic observations cannot guard cached actions.

This implementation covers Chromium's main document and exposed open shadow DOM
controls. Frame targeting and actions on visual-only controls remain future work.
The semantic judge receives only the fresh full snapshot and URL; the visual judge
receives fresh pixels instead. Neither receives node tables, diffs or actor history.

Node references are temporary execution handles. For replay/export, the engine
can translate an action into an ordinary role/name locator only after proving that
it uniquely identifies the same physical element. Ambiguous controls still work
live, but their actions produce an explicit export barrier and bypass the cache.
Transient node and observation IDs remain in execution evidence, never in persistent
replay recordings or generated selectors. Fill actions retain the existing cache
exclusion even when a unique locator is available.

### Action replay cache

The CLI defaults to `--cache auto`. The first successful structured browser
journey records eligible action steps. A compatible later run dispatches those
actions through the engine without an acting-model call. An independent goal judge still checks
the resulting screen before the step advances. Exact assertions and
semantic and visual judges always run again; discovery/planning still uses its normal agent.
Cache reuse does not reuse a prior verdict or establish coverage of hidden state.

```sh
ov "Verify the cart" --headless --cache auto
ov "Verify the cart" --headless --cache strict
ov "Verify the cart" --headless --cache refresh
ov "Verify the cart" --headless --cache off
```

| Mode | Behavior |
| --- | --- |
| `auto` | Replay compatible steps; use the actor on a miss or staleness detected before the current step dispatches any cached action. Save only after fresh assertions and cleanup pass. |
| `strict` | Require compatible recordings for act steps. Never call the actor or modify cache storage; semantic and visual judges still run. Missing, stale or unsupported recordings block the case. |
| `refresh` | Run every act live and replace its recording only after the case passes verification and cleanup. |
| `off` | Do not read, write or invalidate recordings. |

Recordings live under the OS user cache directory for `open-verify`, in `replay/`,
separated by a hash of the canonical project path. Override the root with
`--cache-dir PATH`. Cache files are local execution inputs, excluded from run
bundles and publication attachments. They contain action arguments and screen
hashes, not screenshot bytes, raw screen text, actor summaries or judge verdicts.
Files are bounded to 2 MB and replaced atomically with owner-only permissions.
Remove the relevant project cache directory to clear it.

The key includes the exact journey (instructions, URLs, assertion definitions and
budgets), completion checks, prerequisites, engine replay revision, runtime/platform,
origin policy and tool schemas. Case IDs and titles do not determine identity.
There is no fuzzy matching: reworded goals or assertions and changed entry ports
produce misses. A live planner can propose different wording on repeated requests;
identical request text alone therefore does not guarantee a cache hit. Temporary
PR checkouts have distinct project namespaces and do not share recordings.

Replay checks a fresh URL, full accessibility snapshot and canonical semantic
control state before each action, then checks the resulting screen afterward.
Transient IDs and diffs do not affect these hashes. Screens must match exactly.
Dynamic text can invalidate recordings; truncated snapshots or semantic tables
are ineligible. Local engine replay revision 2 makes older recordings miss. The engine still
validates current arguments and enforces its origin policy. If a screen or action
fails after dispatch begins, the runner blocks with `REPLAY_STALE` and does not
repeat the step through the agent. Auto mode discards that recording. A fresh
assertion failure keeps its actual failure and invalidates the recording, without
an agent retry that could conceal the regression. Strict mode reports these
invalidations while leaving stored files unchanged.

This first version bypasses authenticated journeys and cases containing field
fills, ambiguous node actions, failed actions or incomplete observations. This avoids persisting login
state or filled values as a new cache surface. Strict mode cannot run such act
steps without eligible recordings. Missing records use `REPLAY_MISS`; unavailable
replay support uses `REPLAY_UNAVAILABLE`. An engine must expose a JSON-compatible
`replay_identity()` to opt in and bump its revision when action/observation
semantics change. The default local engine implements that contract.

Each step receipt reports `cache` (`hit`, `miss`, `stale`, `refresh`, `bypass` or
`off`), its reason and actual model/action counts. `journeys/<case-hash>.json`
includes cache events and the recording key; `evidence.jsonl` and `actions/`
retain dispatch and cache receipts. Successful hits do not rewrite storage.
Storage write failures are reported without changing a verified test outcome.
Manifest schema 1 and replayed Playwright exports remain compatible.

Programmatic runners stay uncached unless supplied with
`replay_cache=ReplayCache(directory, project)` and an optional `cache_mode`.
Both `VerificationRunner` and `JourneyRunner` accept those options.

Custom reasoning can be supplied with `journey_executor=...`, implementing
`begin`, `act(context, on_call=...)` and `judge(instruction, observation,
on_call=...)`. Call `on_call` immediately before every model request so repairs
consume the same budget. A model is unnecessary for assertion-only exact journeys.

To supply another executor, pass `executor=...` to `VerificationRunner`, with
`None` in place of the default prompt-based agent. Implement
`async decide(context: DecisionContext) -> Decision`; a model is optional. Supply
an engine implementing `Engine` alongside it. Application actions remain checked
and recorded by that engine. The default local engine uses `ActionDispatcher` so
its capability declaration and dispatch policy cannot drift apart.

`BrowserRunner` is a whole-journey test execution interface, distinct from the
lower-level `Engine` action interface. Existing imports of
`workflow.Verification` and `tools.LocalTools` remain compatibility aliases for
`VerificationRunner` and `LocalEngine`.

Supporting modules retain their roles:

- `changes.py` and `scope.py`: revision inspection and discovery policy.
- `models.py`, `journey_spec.py` and `test_spec.py`: typed decisions, steps, replay traces and login requests.
- `media.py`: bounded GIF summaries and legacy MP4 conversion.
- `manifest.py`: versioned OE contract and attachment validation.
- `procedures.py`: frozen procedural guidance used by the default executor.

See [the migration plan](PLAN.md) for implementation stages and verification.

This is an initial implementation of procedural guidance inspired by
[Procedural Graphs](https://arxiv.org/abs/2609.09153), not the paper's full learning system.
There is no automatic refinement, graph database, separate guidance model, or dependency
on Google's models. Plans, observations and decisions remain the same for every provider.

The local runner is **not a security sandbox**. The ACP provider is also an external
process with its own configuration; cancelling unexpected native tool calls cannot undo
actions already performed by that process. Use trusted repositories. Isolation belongs
in a future Docker runner. A full run can mutate application test data as normal QA does.

Not implemented yet: durable resume, interactive terminal sessions, popup/multi-tab
browser workflows, automatic graph evolution, Docker, Maestro, and automatic
before/after revision environments.

## Manual QA coverage

`ov` defaults to manual QA through the actual application. A backend-only diff
still needs a basic browser journey for an available web app, or actual commands
for a CLI app. The host discovers runnable entry points from checkout manifests
independently of the changed files and rejects plans that omit that application
smoke. With the default `--max-cases 1`, prioritize a relevant user journey:
create/open disposable data, assert the visible result, and reload/reopen it.
Browser journeys retain screenshots/GIFs; CLI journeys retain commands and output.
Opening a page, starting a process, checking health, or running a test suite alone
is insufficient.

Plan cases use `interaction=user` for application UI/API/CLI actions and
`interaction=library` for direct in-process public-API assertions. Library checks
are supporting coverage when a runnable application exists, even if only library
files changed. Add them with additional case scope rather than replacing the
application smoke. A genuinely library-only project can use them as its primary
coverage. `--verification tests` explicitly permits existing repository suites.

If startup, credentials or another prerequisite blocks the application journey,
retain its logs and report it blocked. Passing supporting checks cannot replace
that result. Startup and QA environment recipes come from the tested checkout;
use `--setup-file OV.md` and explicit helper paths when local recipes are absent
from the PR. Private setup files are never copied implicitly.

## Independent application health

Manual browser smoke requires a structured journey. Before browser cleanup, including
when assertions fail or actions are blocked, a host-required observer reads a fresh
screen and new output from managed servers captured since the journey began. It uses a fresh judge conversation without the
actor's transcript or success claims. An unexpected failed job, error banner or
server exception blocks the case for investigation even if prompt/URL assertions
passed. Existing errors in the pre-action log baseline are not new failures;
truncated, rotated or unavailable required evidence cannot establish a pass.
Expected negative-test outcomes are judged in context, not by keyword matching.
The observer receives failed assertion definitions and host results as evidence, without
actor conclusions. An evidenced fixture failure (`FIXTURE_ERROR`) or mistaken assertion
representation (`ASSERTION_INVALID`) blocks the run for QA repair. Healthy observations
cannot turn a failed requirement into a pass. Original failures and all unexecuted steps
remain in the saved journey; reports lead with the diagnosis. Cancellation stops model work.

For creation that starts background work, plan and assert a documented healthy
state appropriate to that application, then check it again after reload when relevant.
Core guidance does not prescribe particular workflows, status names, labels or provider protocols.
Those belong to the target project's documentation or explicit setup files (such as `OV.md`).
If using substitutes, inspect their documented request/response and completion contracts;
arbitrary text responses are not valid substitutes. Readiness verifies the actual prerequisites
of the chosen user journey through appropriate observable interfaces.
Declare `journey.scripted_providers=true` for scripted responses; this requires at least one
`journey.setup_probes` command. Each probe has `instruction`, `argv`, optional `cwd`, `stdin`
and `timeout`. Before browser readiness or user actions, the host executes these fixed
commands under the normal `--allow-exec` policy and requires exit 0 without a timeout.
Use the target integration's actual request matching, response validators and completion
contracts against the fixture file. Guessed selectors or a populated UI selector are insufficient. Failed probes retain command/log
receipts and allow the existing two setup repair opportunities with unchanged probes/checks.
Prefer a complete shipped fixture; do not add catch-all success responses to mask mismatch.

`expect_text` defaults to `match="exact"`. Use an observed full path for exact equality,
or explicitly select `match="contains"` when a path suffix is the intended requirement:
`{"kind":"expect_text","text":"sample-resource","match":"contains"}`.
Both live execution and exported replay preserve this choice; failures never switch modes.
A broken fixture is a blocker, not proof that the PR introduced a product defect.

The final health checkpoint and its log/screen receipts appear in the report and
manifest. The exported browser trace stops at a verification barrier: exact UI
replay cannot claim the live health observer's result. Standalone legacy browser
checks remain available outside the required manual application smoke.

## Backend checkpoints

Backend lifecycle commands can declare `expect.checkpoints` as an ordered list
of planned completion check names, each mapped exclusively to that command in
`checks`. After an assertion succeeds, the script prints a flushed line such as:

```text
OV_CHECKPOINT {"check":"Unapproved proposal stays out of the queue","detail":"queue=[]"}
```

The runner validates the event names, order and payloads from the complete log.
It assigns checkpoint statuses, retains earlier completions if the command fails,
and marks unreached checks as not run. Missing or malformed events block the case;
exit code and output assertions must still pass. HTTP and legacy command suites
also receive checkpoints derived from their mapped operation results. Checkpoints
appear in CLI progress, reports and the manifest; the exported test preserves the
same protocol for replay.

Generated Python assertion scripts use `argv=[python, "-"]`, source in `stdin`,
`expect.python_harness=true`, and expected exit code zero. The host wraps the source:
uncaught `AssertionError` produces a failed assertion with its message; other
exceptions produce a blocked `HARNESS_ERROR` with the exception and retained traceback.
A crashed harness does not establish a product failure. Product CLI commands retain
normal exit-code assertions, including expected nonzero exits.

One diagnosed harness repair is allowed through `retry_reason`. Only stdin source
may change: the plan, step mapping, commands and expectations stay fixed. Both
attempts and their result files remain in the export; the manifest shows the latest
result. Assertion failures, HTTP/CLI failures, timeouts and policy refusals do not
receive retries. Use JSON-native mocked provider responses; `dataclasses.asdict()`
retains enums, so explicitly serialize their `.value`, including nested values.

These events report the script's assertions, not a
separate independent judge of the Python harness.

## Tests

From this directory, with its environment activated:

```shell
python -m pytest
```

Tests exercise the real Python runner, custom executor/engine contracts, a local ACP subprocess fixture, HTTP/terminal
execution, failure reporting and browser interaction against a local fixture page.
Browser tests skip if the optional package or Chromium is unavailable. They do not
consume provider credits; real Codex/Claude compatibility still needs a live smoke run.


## Bounded repository inspection

The planner and each case see an index of source paths, read offsets, pagination
hints, missing files and evidence IDs, even after an agent conversation reset.
Repeated identical `read_file`, `list_files` and `read_change_diff` requests return
`ALREADY_INSPECTED` with retained evidence; they still consume decision/inspection
allowance. A missing `OV.md` does not need repeated checks. Setup/application
operations clear this read cache because files may have changed. For an external
edit, `read_file`/`list_files` accept explicit `refresh=true`; this does not bypass
inspection limits or the engine's path/secret checks.

`read_file` accepts `path`, `offset` (Unicode characters, default 0) and `limit`
(1–24000, default 24000). A truncated result includes `next_offset`; request that
section instead of rereading the prefix. The host bounds offsets to 10 million
characters and keeps the existing dependency/metadata/secret-file exclusions.
The cache is a run-local inspection aid, not a test result or application replay.


## Evidence sources and project-specific setup

The core owns execution, evidence collection, independent assertions, budgets, diagnosis and
reporting. Product-specific startup recipes, fixture protocols, labels, healthy states and smoke
journeys come from the tested project's documentation or explicitly supplied setup files.
OpenEngine examples elsewhere in this document illustrate one integration; they are not core defaults.

A semantic assertion, including a readiness check, may declare up to three read-only HTTP sources:

```json
{
  "kind": "assert",
  "instruction": "The visible Sample workspace corresponds to workspace id w-17 in the configuration",
  "evidence_requests": [{"url": "/api/workspaces"}]
}
```

The host supplies fresh screen text plus HTTP receipt IDs, URLs, status and body to the independent
judge without actor conclusions. Relative URLs bind to the journey's application origin; absolute
HTTP(S) URLs obey the same engine origin policy as ordinary requests. These requests accept no
credentials, custom headers or mutating methods. Missing or truncated required evidence blocks
judgment; a non-success HTTP status remains explicit evidence for the stated requirement.
A relation must have an evidenced shared key. No automatic path rewriting or label-to-ID guesses
are applied. Exact assertions and visual judgments cannot declare additional HTTP sources.
Exported replay retains a live-verification barrier for these semantic checks. It cannot claim
that replaying UI actions has re-established a relationship to backend identity.


## Learned product onboarding

QA skills stay in OV. Product onboarding lives separately in readable JSON under `.ov/`.
OV loads the profile before discovery so the planner can reuse product terminology, basic
workflows and environment guidance. Prior knowledge is advisory evidence, not instructions,
permission to execute commands, or proof that the current application passes.

After a first onboarding run, the built-in ACP agent gets one bounded learning pass (the configured ACP response timeout, capped at 180 seconds,
at most two provider requests including repair). The evidence packet is capped at 24,000 characters and prioritizes completed journeys and product
documentation over repeated screen dumps. It can propose up to 20 stable facts, separated
into `product`, `workflow` and `environment`, plus uncertainties. Every fact cites current host
receipts. The host validates those references and records source paths/hashes, evidence IDs,
confidence (`documented`, `inferred`, `observed`), timestamp, revision when known and the original
run location. These labels express evidence basis, not a guarantee that every model inference
is correct. Review the generated profile. Failed UI runs and process logs are not onboarding
sources; observed workflows require a passed host journey. Common credential payloads and
disposable paths/IDs are rejected, but this is not a general secret scanner: do not put secrets
in product onboarding documents.

Local runs use `<project>/.ov/product.json`. PR runs retain knowledge in the original caller's
project under `.ov/repositories/<repository-hash>/product.json`, outside the disposable checkout.
Each GitHub target repository has a separate identity. This first version keeps local-run and
PR-run profiles separate. `.ov` is excluded from ordinary discovery/change inspection, and
profiles are not automatically added to published test evidence.

Code/documentation hashes mark changed-source facts stale. Observed facts tied to a known PR
revision become stale on another revision. UI-only facts in runs without revision information
remain advisory and require fresh testing; OV does not automatically detect every UI change.
Supported refreshes update the provenance of unchanged statements; conflicting statements are
retained for review rather than silently replacing expectations. Existing invalid or mismatched
profiles are left untouched. Writes are atomic and symlink profiles are refused.

Use `--refresh-knowledge` to request another bounded learning pass from evidence collected in
that run, or `--no-knowledge` to disable both loading and learning. Normal runs with a current
profile skip the additional learning call. Learning does not delay beginning useful QA or consume
its action/decision budget; it runs after the QA report is saved. A learning error cannot turn a
passing test into a failed test. `knowledge-status.json` records timeout, elapsed time, request count
and input size when learning fails; missing or empty profiles are retried on the next run. Custom executors without a structured response transport can
consume saved knowledge but do not automatically generate it in this version.

```bash
ov "Check the main user journey" --allow-exec
ov "Check search after this change" --allow-exec
ov "Check search and refresh onboarding" --allow-exec --refresh-knowledge
```

Reports show a visible coverage limit when no passing case verifies changed behavior, and disclose
scripted providers beside the summary. A successful regression smoke does not establish PR coverage
or live external-provider connectivity.


Action completion is independently checked against a fresh screen, the initial observation,
and host-executed operations. The actor's summary is excluded. A contradicted goal permits one
recovery within the original action, acting-call and time budgets. Missing or truncated evidence
blocks completion (`ACTION_GOAL_UNCONFIRMED`); a second mismatch stops the step
(`ACTION_GOAL_NOT_REACHED`). Cached actions also require a fresh goal
check. Exported replay stops at a goal-verification barrier instead of claiming the model check
was reproduced. Fixed case assertions remain independent and unchanged.

Health diagnosis distinguishes `action`, `fixture`, `assertion`, `application` and `unknown` causes.
Wrong navigation is a QA action problem when evidenced, not proof of an application bug.
Unattributed failures remain blocked with `APP_HEALTH_INCONCLUSIVE`; `UNEXPECTED_APP_ERROR`
requires an application diagnosis. Neither recovery nor a healthy final screen erases failed checks.


Action-step `max_model_calls` limits the actor (including protocol repair). The host reserves
up to two provider requests per independent goal check, at most four across the initial check
and one recovery. Goal checks share the original step deadline and never grant more tool actions.
Receipts count all actor and goal requests in `model_calls`; exhausting the independent allowance
blocks with `ACTION_GOAL_MODEL_LIMIT`. A two-call reload step can therefore perform its reload,
conclude, and still receive independent verification. Blocked report captions also include up to
three passed product checkpoints, so partial execution is visible without implying a complete pass.


Manual browser journeys require `journey.entry`: current source/UI evidence IDs and independent
`controls` assertions for the first action. `journey.url` supplies the supported direct entry route.
The first LangGraph stage has two nodes: `validate_entry_evidence` → `verify_entry_readiness`.
Typed state carries the case, whether an entry is required, and host readiness results; model
conversation history is not shared through this state. Other QA orchestration remains in the
existing runner. This graph has no durable checkpointing or automatic browser/process resume.

Failed readiness returns to the planner with at most two repair opportunities. The
`repair_journey_entry` tool can correct only the current case's route and cited evidence before
execution. It preserves the application origin, existing entry controls, setup probes, goals and
case assertions. A missing legacy contract may be supplied during this pre-execution repair.
Neither the presence of a citation nor saved product knowledge proves that a route is correct:
live controls must pass, including a fresh check in the case's actual isolated browser session.
Navigation-only entry paths must first be resolved to a supported direct route in this version.
Library/custom journeys without a detected runnable application retain optional entry contracts.

Entry blockers remain the reported cause when no user actions were exercised. Application health
can inspect the current page, but it cannot demand completion or persistence of unexecuted work.
