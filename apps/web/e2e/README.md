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
Python process serves Vite's output and a stale `dist/` would mean testing the
previous commit's interface.

## How it is put together

Each test gets its own application, composed by `harness/server.py` exactly the
way `engine.apps.web.__main__` composes the real one -- same capabilities, same
runner mapping, same approval policy plumbing. Only four things are the test's,
and each is something a test run must not share or send anywhere:

| what | why |
| --- | --- |
| a fixture git repository, and a bare `origin` beside it | conversations and runs make worktrees of it, and a run bases its worktree on `origin/main` |
| a SQLite file under the test's own directory | one test's chats must not be another's |
| scripted `codex` and `claude` executables | a model is the one part of this that cannot be asserted on |
| a `gh` that records instead of commenting | the reviewer leaves its findings on a pull request, and that is somebody's repository |

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
step. The first matching scenario wins, which matters for a workflow: the
reviewer is quoted the original task, so its prompt contains the implementation
scenario's word too, and the one only a reviewer can match has to be listed
first. A turn run without the approval transport -- the runtime naming a chat or
a workflow -- is answered with `title` instead of a scenario.

A workflow step ends only when the agent calls `complete_step` or `fail_step` on
the run-bound MCP server the runtime attached to that turn, so the fakes are MCP
*clients* too:

```ts
{ type: "tool", name: "complete_step",
  arguments: { outcome: "success", summary: "Added the greeting.",
               outputs: { pr_url: "https://github.com/acme/api/pull/7" } } }
```

They read the server off argv the way each provider encodes it -- `--mcp-config`
for Claude, `-c mcp_servers.workflow.*` or the app-server thread config for
Codex -- spawn it as given, and make a real JSON-RPC `tools/call`. A completion
missing a declared output is refused by the runtime and the turn is corrected,
which `tests/test_workflow_integration.py` covers at the faster tier.

**End a workflow scenario on its terminal call.** That is the shape the step
instructions ask for, and it used to be the one that broke: the runtime cancels
the CLI as soon as it accepts a terminal result, but when the CLI finishes first
both adapters assemble the turn with its *last spoken text* as the answer, which
moves narration to the end. The runtime compared that against what it had
streamed by position and refused a step it had already accepted
(`streamed workflow transcript does not match completed turn`). It now matches
streamed messages by identity, so reassembly order is not load-bearing -- see
`test_a_turn_ending_in_its_terminal_call_is_kept_in_streamed_order` in
`tests/test_workflow_mcp_execution.py`, which covers it without the race.

Scenarios here used to carry a closing `say` to keep that race out of the tests.
They no longer do, and adding one back would hide the shape this tier is best
placed to exercise.

A failing test keeps its directory and prints the path, and attaches whatever
the server said to the report. `npx playwright show-trace test-results/…` opens
the trace.

## Reading a run afterwards

Every run writes `playwright-report/`, pass or fail, and each spec attaches a
full-page still at every state it asserts on:

```ts
await shot(page, testInfo, "2 the approval, pending");
```

Numbered so the report reads as a sequence. They are documentation as much as
diagnostics -- what the approval card actually looked like on that commit, for
someone who is not going to run the tier -- so every spec added here should
attach the same kind of still at its own decisive moment.

```
npx playwright show-report apps/web/playwright-report
```

In CI the report is uploaded from the `browser` job whether or not the run went
green. It is a static site and GitHub will not serve it: download the artifact,
unzip it, and point `show-report` at the directory.

## What is covered

* `chat-approvals.spec.ts` -- a new chat on each runner: the turn streams while
  it is still running, the approval it pauses on reaches the browser, approving
  it is recorded as an approval, the turn carries on, and the file the command
  was allowed to write exists in that chat's worktree.
* `approval-placement.spec.ts` -- *where* a request is shown, on each runner: a
  step runs three `git_subcommand` calls through the run-bound MCP server -- two
  different commands and then a repeat of the first -- and each pause has to
  render beside the call that raised it, with nothing collecting in the
  end-of-turn slot, before the decision, after it, and after a reload. The
  pairing is the provider's own id for the call, which that server is the one
  place that has to look up rather than know; the lookup itself is pinned at
  speed in `tests/test_workflow_mcp_execution.py`, and what only a browser can
  say is that the pairing survives everything between the broker and the page.
* `workflow-run.spec.ts` -- a workflow run on each runner, end to end: the run
  is created from the form, provisions a checkout that exists on disk, streams
  the implementation's first message and its command into the step's
  conversation *while the step is still running*, and -- once `complete_step`
  carries the declared `pr_url` -- advances through a review that leaves its
  finding on `gh` to "Action required". Approving there ends it, and a reload
  shows the same finished run: `succeeded`, `approved`, every stage behind it.
* `graph-workflow.spec.ts` -- the same journey as `workflow-run.spec.ts`, for a
  `[BETA]` graph WorkOrder, and **expected to fail in places**. See below.
* `persisted-navigation.spec.ts` -- cold starts over both a SQLite file populated
  through the current production state-store adapter and the frozen
  `fixtures/v0.0.0.sqlite3` artifact. The run list, run detail, implementation
  and review transcripts, and a multi-turn standalone chat are followed through
  their browser links. Each case then starts another chat and another workflow
  in the same database and confirms the older history remains listed. The frozen
  artifact makes opening the database exercise migrations added after v0.0.0.

## The `[BETA]` graph WorkOrder, and what it cannot do yet

`graph-workflow.spec.ts` is `workflow-run.spec.ts` walked again, on the other
engine: the same task, the same four stages, but started from a `[BETA]` entry
and run by LangGraph. It is split into one test per state a run passes through,
because a graph WorkOrder does not reach all of them yet and one long test
would report only the first gap.

```
npm --prefix apps/web run test:e2e:beta     # only these
npm --prefix apps/web run test:e2e          # everything else
```

They are tagged `@beta` and run by their own CI job, which is allowed to fail.
Each red test names one thing the interface cannot do for a graph run that it
already does for a step run. As of this writing:

| state | passes? |
| --- | --- |
| the graph is offered, and the form does not ask for a runner | yes |
| the run provisions a checkout and both agents work in it | yes |
| the WorkOrder page shows the run's stages | no |
| the checkout is named on the WorkOrder page | no |
| an agent's conversation is readable from the page | no |
| the page says a person is being waited for, and can answer | no |

The last one is the important one: the run *does* reach its human-review node
and *does* raise an approval — the test polls the graph engine's own API to
prove it — and there is simply nothing on the page to answer it with.

The agents are scripted the same way as everywhere else here, but over a third
protocol. `ACPNode` talks ACP to an adapter that wraps a CLI, not to the CLI, so
`provider_fakes.fake_acp` is an ACP agent reading the same script. A graph node
ends by finishing its turn rather than by calling `complete_step`, so a `tool`
step is refused in an ACP scenario rather than ignored. `harness/server.py`
rebuilds the repository's graphs through `graph_for(runner, ...)` to point them
at that agent and at the test's own worktree root; the ids, names, stages and
prompts are the shipped ones.

## What the rest needs
Everything else -- workflow runs, review, reactivation, auto-approve, failure,
plans -- is written up as tickets in **`TICKETS.md`**, in dependency order, with
the files each one touches and what "done" means.

The behaviours below are the ones we want next. Each names what has to exist
before it can be written; nothing here is a change to the product, except where
it says so.

### Workflow runs

1. **Questions.** Both providers can ask: Claude through `AskUserQuestion`,
   Codex through `item/tool/requestUserInput`. Both already normalize to
   `user_input` approvals with a modal in the client.
   *New script step: `{"type": "ask", "questions": [...]}`.*
2. **Plans.** Only Claude produces `plan_approval` today (`ExitPlanMode`); Codex
   has no app-server equivalent, so that test is Claude-only until it does.
   *New script step: `{"type": "plan", "plan": "…"}`.*

### The behaviours, once those exist

| behaviour | needs | notes |
| --- | --- | --- |
| approval propagates and approving executes | — | same card as the chat test, reached from the run page |
| agent asks for clarification | 1 | answering resumes the same agent run |
| reviewer adds review comments | — | assert against `engine.ghLog`, not GitHub; the reviewer is refused `complete_step` until it has left at least one comment |
| talking after review reopens implementation | — | `StepReactivated`; the composer is only offered on editable steps |
| auto-approve runs several requests unattended | — | toggle in the conversation header; script several `run` steps and assert `decisionSource` is not `user` |
| a failed workflow reads as failed | — | `fail_step`, and a CLI that exits nonzero -- they surface differently |
| a plan reaches the operator | 2 | Claude only |
| rejecting reopens the implementation | — | the correction loop: `Reject`, then `StepReactivated` and a second implementation turn |

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
* `npx playwright show-trace test-results/…/trace.zip` -- the same, after the
  run. Locally every test keeps a trace, passing ones included, so the click
  that approved something can be replayed later; CI keeps failures only.
* `npx playwright codegen <url>` -- point it at a harness server you started by
  hand (`uv run python apps/web/e2e/harness/server.py --port 8123 --repository
  … --state …`) and click through it to author selectors.
* `ENGINE_E2E_PYTHON=/path/to/python npm run test:e2e` -- skip `uv run` per
  test when you already have a prepared interpreter.
* Specs currently reach for class names (`.approval-pending`, `.stat`). A small
  number of `data-testid` landmarks in the client would make them read better
  and break less; worth doing when the second or third spec wants the same
  element.
