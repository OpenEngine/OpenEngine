# Browser end-to-end tests

What a person actually does, done by a browser: open the interface, send a
message, watch a turn arrive, answer what it stops to ask, and check that what
was allowed actually happened.

```
npm --prefix apps/web ci
npx playwright install chromium        # once per machine
npm --prefix apps/web run test:e2e
```

The client is built for you before the run (`e2e/build-client.ts`), because the
Python process serves Vite's output from inside its own package and a stale
`src/engine/apps/web/client` would mean testing the previous commit's
interface.

## How it is put together

Each test gets its own application, composed by `harness/server.py` exactly the
way `engine.apps.web.cli` composes the real one -- same capabilities, same
runner mapping, same approval policy plumbing. Only three things are the test's:

| what | why |
| --- | --- |
| a fixture git repository | conversations and runs make worktrees of it, and a temporary directory is a safe thing to leave worktrees in |
| a SQLite file under the test's own directory | one test's chats must not be another's |
| scripted `codex` and `claude` executables | a model is the one part of this that cannot be asserted on |

The fake CLIs are `tests/provider_fakes.py`, shared with the pytest tier that
runs the approval contract against them. They are not mocks of our adapters:
they are real subprocesses speaking Codex's app-server JSON-RPC and Claude
Code's stream-JSON control protocol, and they really run the commands they are
allowed to run. What a turn says and does comes from a JSON script the test
writes:

```ts
engine.script({
  title: "Recording an approval",
  scenarios: [
    {
      when: "greeting",                       // matched against the prompt
      steps: [
        { type: "say", text: "Reading the repository first." },
        { type: "run", command: "echo approved > allowed.txt" },
        { type: "say", text: "Wrote the file." },
      ],
    },
  ],
});
```

Scenarios are selected by what the turn was asked rather than by a counter, so
a title turn, a retry, or a second conversation cannot knock a script out of
step. A turn run without the approval transport -- the runtime naming a chat or
a workflow -- is answered with `title` instead of a scenario.

A failing test keeps its directory and prints the path, and attaches whatever
the server said to the report. `npx playwright show-trace test-results/…` opens
the trace.

## What is covered

* `chat-approvals.spec.ts` -- a new chat on each runner: the turn streams while
  it is still running, the approval it pauses on reaches the browser, approving
  it is recorded as an approval, the turn carries on, and the file the command
  was allowed to write exists in that chat's worktree.

## What the rest needs

The behaviours below are the ones we want next. Each names what has to exist
before it can be written; nothing here is a change to the product, except where
it says so.

### Workflow runs (provision → implement → review)

1. **An `origin` to base a run on.** `WORKFLOW_BASE_REF` is `origin/main`, and
   provisioning fetches `+refs/heads/main` from it. The fixture repository has
   no remote, so the harness needs a bare origin beside it and a first push.
   *Small; harness only.*
2. **The fake CLIs must speak MCP as clients.** A workflow step ends by calling
   `complete_step` (or `fail_step`) on the run-bound MCP server the runtime
   attaches, and nothing else ends it: no `complete_step`, no review step, and
   after two corrections the run fails. The fake therefore has to read the
   server it was given -- `--mcp-config` for Claude, `-c mcp_servers.workflow.*`
   for Codex -- spawn it, and make a JSON-RPC `tools/call`. This is the single
   largest prerequisite, and it unblocks 1e, 1f, 2, 3, and 4 below.
   *New script step: `{"type": "tool", "name": "complete_step", "arguments": …}`.*
3. **A `gh` that is not GitHub.** The reviewer's `add_comment` goes through
   `GitHubSourceControl`, which shells out to `gh`. The binary is not a
   `Settings` field, so the harness puts a fake `gh` first on `PATH` for the
   server process and asserts on what it was called with. Note the reviewer is
   refused `complete_step` until it has left at least one comment, and that it
   needs a `pr_url`, which is a declared output of the implementation step.
4. **Questions.** Both providers can ask: Claude through `AskUserQuestion`,
   Codex through `item/tool/requestUserInput`. Both already normalize to
   `user_input` approvals with a modal in the client.
   *New script step: `{"type": "ask", "questions": [...]}`.*
5. **Plans.** Only Claude produces `plan_approval` today (`ExitPlanMode`); Codex
   has no app-server equivalent, so that test is Claude-only until it does.
   *New script step: `{"type": "plan", "plan": "…"}`.*
6. **A human decision has no button.** `POST /api/runs/{id}/human-review` exists
   and the run page shows "Action required", but nothing in the client calls it.
   A test that drives a run to its end through the browser needs that control to
   exist first. *A product change, not a test one.*

### The behaviours, once those exist

| behaviour | needs | notes |
| --- | --- | --- |
| workspace provisioned, run reaches implementation | 1 | assert the run page's stages, and that the worktree exists on disk |
| conversation streams tool calls and messages | 1 | the workflow conversation streams by polling the durable transcript, so assert through `/runs/{run}/conversations/{instance}` |
| approval propagates and approving executes | 1, 2 | same card as the chat test, reached from the run page |
| agent asks for clarification | 1, 2, 4 | answering resumes the same agent run |
| `complete_step` advances to review | 1, 2 | assert the review step starts, and on which runner |
| reviewer adds review comments | 1, 2, 3 | assert against the fake `gh`, not GitHub |
| talking after review reopens implementation | 1, 2 | `StepReactivated`; the composer is only offered on editable steps |
| auto-approve runs several requests unattended | 1, 2 | toggle in the conversation header; script several `run` steps and assert `decisionSource` is not `user` |
| a failed workflow reads as failed | 1, 2 | `fail_step`, and a CLI that exits nonzero -- they surface differently |
| a plan reaches the operator | 1, 2, 5 | Claude only |

## Live provider CLIs

This tier is deliberately deterministic: a scripted CLI is what makes "the
agent asked, the user approved, the file exists" a fact about our code rather
than about a model's mood. The live half already exists and belongs where it
is: `.github/workflows/cli-compatibility.yml` runs the same approval contract
against the pinned real `codex` and `claude` releases on a schedule.

If you want the browser tier pointed at a real CLI as well, the credentials go
in **repository → Settings → Secrets and variables → Actions**, under the names
that workflow already reads:

* `OPENAI_API_KEY` -- Codex CLI.
* `ANTHROPIC_API_KEY` -- Claude Code. A subscription token from
  `claude setup-token` works too, as `CLAUDE_CODE_OAUTH_TOKEN`; whichever you
  add, the job must export it into the server process's environment, because
  that is what spawns the CLI.

Absent, live scenarios skip rather than fail: an unauthenticated runner is a
configuration fact, not a test result. Nothing in this directory reads a
credential today.

## Tooling worth knowing

* `npx playwright test --ui` -- the run, the DOM, and the network at each step.
* `npx playwright show-trace test-results/…/trace.zip` -- the same, after a CI
  failure. Traces are kept on failure only.
* `npx playwright codegen <url>` -- point it at a harness server you started by
  hand (`uv run python apps/web/e2e/harness/server.py --port 8123 --repository
  … --state …`) and click through it to author selectors.
* `ENGINE_E2E_PYTHON=/path/to/python npm run test:e2e` -- skip `uv run` per
  test when you already have a prepared interpreter.
* Specs currently reach for class names (`.approval-pending`, `.stat`). A small
  number of `data-testid` landmarks in the client would make them read better
  and break less; worth doing when the second or third spec wants the same
  element.
