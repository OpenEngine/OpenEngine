# Open Verify

A standalone CLI for exploratory QA of a Git project. Describe a feature; Open Verify
inspects the project, proposes test cases, exercises its browser, terminal or HTTP
interface, and saves a report with captured evidence. It has no web UI or service.

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

Activate the environment, change into the Git project to test, and run:

```shell
open-verify "Verify login accepts valid credentials and rejects an invalid password" --plan-only
open-verify "Verify API pagination, including empty and invalid page values" --agent claude --allow-exec
open-verify "Verify the contact form shows validation errors" --agent codex --allow-exec
```

Running `open-verify` without a request prompts for one in an interactive terminal.
`--project PATH` selects another project's directory. Discovery finds the enclosing
Git root, including worktrees; it does not invoke Git or change branches.

Useful options:

- `--model ID`: select a model supported by the chosen provider.
- `--agent NAME --agent-command '["executable", "--acp"]'`: another ACP provider.
- `--plan-only`: file discovery and a plan; no host command, HTTP or browser actions.
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
`report.json`, `report.md`, process logs, and browser screenshots/trace when used.
Reports cite evidence IDs. These local artifacts can contain application data,
request bodies and headers; use disposable test accounts and protect the run directory.
Browser assessment currently uses accessibility snapshots and visible-text checks;
screenshots are saved for human review, not sent to the model for visual evaluation.

Exit codes: **0** = all cases passed, or a plan-only run completed; **1** = at least
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

This is an initial implementation of procedural guidance inspired by
[Procedural Graphs](https://arxiv.org/abs/2609.09153), not the paper's full learning system.
There is no automatic refinement, graph database, separate guidance model, or dependency
on Google's models. Plans, observations and decisions remain the same for every provider.

The local runner is **not a security sandbox**. The ACP provider is also an external
process with its own configuration; cancelling unexpected native tool calls cannot undo
actions already performed by that process. Use trusted repositories. Isolation belongs
in a future Docker runner. A full run can mutate application test data as normal QA does.

Not implemented yet: durable resume, interactive terminal sessions, popup/multi-tab
browser workflows, automatic graph evolution, Docker, and generated regression suites.

## Tests

From this directory, with its environment activated:

```shell
python -m pytest
```

Tests exercise the real LangGraph loop, a local ACP subprocess fixture, HTTP/terminal
execution, failure reporting and browser interaction against a local fixture page.
Browser tests skip if the optional package or Chromium is unavailable. They do not
consume provider credits; real Codex/Claude compatibility still needs a live smoke run.
