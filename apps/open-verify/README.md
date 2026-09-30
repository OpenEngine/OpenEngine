# Open Verify

A standalone CLI for exploratory QA of a Git project. Describe a feature or point it
at a change; Open Verify inspects the project, plans and exercises its browser,
terminal or HTTP behavior, and saves evidence. Change mode exports runnable Playwright
tests and relevant screenshots/video for OE. It has no web UI or service.

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
change mode uses read-only Git commands and never fetches or changes branches.

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
Browser journeys compile into ordinary Playwright Python tests with explicit assertions;
the host executes the exact generated code in a fresh, origin-guarded browser. Findings
must match the generated test's actual result. Terminal/HTTP cases retain exploratory
execution and receipts; generated suites currently cover browser journeys.

Tests and supporting instructions live in `tests/`. Each exported file runs with
`python tests/test_<id>_<attempt>.py` from the bundle root and an installed
`open-verify[browser]` of the recorded version. Start the target app first; startup
instructions are in `plan.json` and prerequisites in `report.md`. Replay requires no
agent or Git. `OV_BASE_URL` overrides the entry URL, and `--allow-origin` permits
additional origins. Each run starts without existing login state, so necessary UI
setup belongs in the journey. The `test_change(page)` function can also be adopted
into an existing async Playwright suite.

When impact warrants visual evidence, journeys can include named `screenshot` steps
at meaningful UI checkpoints, plus a final screenshot and a short browser recording,
including useful failures. The browser extra includes a bundled MP4 encoder;
**ffmpeg with libx264 on PATH** takes precedence. Encoding is local and bounded; every published video must be
strictly below **10,000,000 bytes**. Missing/failed encoding or a clip that remains too
large results in an explicit omission, while tests/screenshots remain available.
Raw WebM recordings and traces stay local diagnostics. No dependency is installed
automatically. Recording finalization follows [Playwright's context lifecycle](https://playwright.dev/python/docs/videos).

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

### Assisted sign-in

For protected-page testing, run from an interactive terminal with the application's
real local OAuth configuration available. The QA agent can call `assisted_login`:
Open Verify opens a visible browser, clicks the observed sign-in link/button, and
asks you to complete GitHub login and MFA. It waits up to five minutes by default
and resumes automatically when the same-origin JSON session endpoint confirms
`authenticated: true`. Closing the browser or timing out offers retry or skip.
GitHub and its asset origins are enabled only for this private login browser;
other browser tests keep their existing origin policy.

The host holds app cookies/local storage in memory for the run. Generated journeys
with `authenticated: true` receive isolated copies; signed-out cases remain empty.
The session is checked before each authenticated journey, and an expired session
blocks that journey pending another assisted login. Login itself records no video,
screenshots, or traces. State and provider credentials are not included in the bundle.
This is user-assisted authentication, not evidence of autonomous GitHub login.

To replay an authenticated generated test, supply your own private Playwright
storage-state file with `--auth-state /path/to/private-state.json`. The bundle does
not contain that file. Protected-page screenshots/video may show account data;
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
Browser assessment currently uses accessibility snapshots and visible-text checks;
screenshots are saved for human review, not sent to the model for visual evaluation.

Exit codes: **0** = all cases passed, a plan-only run completed, or the change was
skipped with a reason; **1** = at least
one observed defect; **2** = blocked, incomplete, runtime/cleanup error; **130** = interrupted.
Partial reports survive errors and interruption. Managed services and browser resources
are cleaned up on ordinary completion, error or Ctrl-C; forced process termination and
self-detaching child services can require manual cleanup.

## Architecture

Python + LangGraph + `langgraph-acp`, matching OpenEngine's agent stack:

1. LangGraph runs a bounded discover → plan → act/observe → report loop.
2. A live ACP session returns provider-independent JSON decisions. Host adapters own
   the actual QA actions and evidence. Native agent tool calls are rejected when observed.
3. Local adapters handle repository reads, commands/services, HTTP and optional Playwright.
4. `procedures.json` stores a small frozen graph of conditions, guidance and pitfalls.
   The latest action/outcome locates a node; its two-hop neighborhood guides the next decision.

Change verification keeps separate responsibilities:

- `changes.py` and `scope.py`: read-only revision inspection and discovery policy.
- `models.py` / `test_spec.py`: typed provider-independent decisions and journeys.
- `test_codegen.py`: deterministic Playwright source generation from typed steps.
- `playwright_runner.py`: isolated execution and standalone replay through existing guards.
- `media.py`: bounded MP4 conversion and byte-limit enforcement.
- `manifest.py`: versioned OE contract and attachment validation.
- `workflow.py`: impact, planning, execution, and evidence-backed findings in LangGraph.

`BrowserRunner` is the adapter boundary for future runners. No provider-specific
logic enters test generation, media processing, or publishing.

This is an initial implementation of procedural guidance inspired by
[Procedural Graphs](https://arxiv.org/abs/2609.09153), not the paper's full learning system.
There is no automatic refinement, graph database, separate guidance model, or dependency
on Google's models. Plans, observations and decisions remain the same for every provider.

The local runner is **not a security sandbox**. The ACP provider is also an external
process with its own configuration; cancelling unexpected native tool calls cannot undo
actions already performed by that process. Use trusted repositories. Isolation belongs
in a future Docker runner. A full run can mutate application test data as normal QA does.

Not implemented yet: durable resume, interactive terminal sessions, popup/multi-tab
browser workflows, automatic graph evolution, Docker, Maestro, generated terminal/HTTP
suites, and automatic before/after revision environments.

## Tests

From this directory, with its environment activated:

```shell
python -m pytest
```

Tests exercise the real LangGraph loop, a local ACP subprocess fixture, HTTP/terminal
execution, failure reporting and browser interaction against a local fixture page.
Browser tests skip if the optional package or Chromium is unavailable. They do not
consume provider credits; real Codex/Claude compatibility still needs a live smoke run.
