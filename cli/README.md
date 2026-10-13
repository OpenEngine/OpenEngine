# cli

```bash
engine go "add a new doc for the out-of-the-box experience"
```

`go` registers the built-in default graph on first use, runs it against the
checkout you are in, and follows it live on a TTY with inline approvals. Its
first line reports the daemon's inferred connected or disconnected mode.
Connected runs finish with a PR URL; disconnected runs report the local branch
and a command to push it later. The selected backend must be running.

```bash
engine go "implement the feature" --spec
engine go review feature/my-change
engine go review https://github.com/owner/repo/pull/42
engine go "update docs" --repo owner/repo --backend mini
engine go "update docs" --local --wait --json
```

`--local` forces disconnected mode on the selected backend. `-i KEY=VALUE`
sets graph inputs, including `adversarial=true`, `implementation_runner`, and
`review_runner`. `--wait` polls without interaction; `--json` emits JSON and
sends the mode banner to stderr. Without `--wait`, non-TTY calls return the run
immediately. Repository selection uses `--repo`, otherwise the current checkout
(path for a local backend, origin's owner/repo for a remote backend), otherwise
the backend's configured repository.

`engine graph`, `engine loop`, `engine node` and `engine backend`: register
graphs, run them once or on a cadence within limits, and steer their
nodes — on this machine's daemon or on another one, such as a Mac mini.

```text
cli/client   engine-graph-cli       the commands; HTTP only, never starts a runtime
cli/service  engine-graph-service   /api/v1 on the daemon, over its LangGraph runtime
```

Execution goes through **langgraph-acp**: every agent node in a registered
graph is an `ACPNode`, one ACP session per node attempt, driven by the daemon's
single `LangGraphRuntime` with durable checkpoints. The service is mounted by
`engine-web`, which is what `engine daemon` runs, so nothing new has to be
started — a daemon that is up already serves it.

## Contract

- A **graph** is a versioned definition: a name within a project, with
  immutable versions. Registering a changed manifest adds a version;
  registering an identical one is a no-op.
- A **run** is one execution of one pinned version. Registering a new version
  never changes a run already started.
- A **loop** creates recurring runs of one pinned version.
- A **node execution ID** identifies one attempt at one node within a run.

Backend commands use the selected backend. List commands (`engine graphs`,
`runs`, `loops`, `nodes`, `agents`, `backends`, `connections`) print a table;
add `--json` for JSON. Other commands print JSON unless `--pretty` is given, except the live run view on a TTY. `engine graph spec` and
`engine loop spec` print the current specifications as Markdown locally, without
a running daemon. Their specifications and the site reference are generated from
the parser’s field definitions, expression rules, and accompanying prose; the
site's CLI reference is generated from the `engine` argument parser. Run
`uv run python scripts/generate_cli_docs.py` from the repository root to
regenerate them; the site build also runs it, keeping the committed CLI
reference when the parser's dependencies are not installed. Tests reject stale
output.

## Backends

```bash
engine backend add mini http://mac-mini.local:4364 --token-env MINI_ENGINE_TOKEN --use
engine backends list --check
engine backend use local
engine graphs list --backend mini      # one-off override; ENGINE_BACKEND also works
```

`local` (`http://127.0.0.1:4364`) always exists. A backend records its URL,
the project names resolve in (`--project`), a default repository (`--repo`),
and the *name* of the environment variable holding its bearer token — never
the token. Without `--token-env`, `ENGINE_SERVICE_TOKEN` is sent.

Reaching a Mac mini: `engine daemon` binds loopback only, so expose it over
something you trust — an SSH tunnel (`ssh -NL 4365:127.0.0.1:4364 mac-mini`,
then add `http://127.0.0.1:4365`) or a tailnet. If the daemon has GitHub login
configured, the CLI authenticates with its `ENGINE_SERVICE_TOKEN`; `/api/v1`
accepts that token and operators, and refuses users who see only some
repositories.

## Graphs

```bash
engine graph add ./graph.yaml
engine graphs list
engine graph get fix-flaky-test --pretty        # or fix-flaky-test@2, g-..., gv-...
```

```yaml
name: fix-flaky-test
description: Find a flaky test and make it deterministic.
inputs:
  instruction: {description: What to fix, required: true}
  area: {default: tests/}
runners: [claude, codex]          # required runners; each must exist on the backend
repository: OpenEngine/OpenEngine # optional default
workspace: {base_ref: origin/main}
nodes:
  - id: implement
    agent: claude
    prompt: "Fix this, looking under {{ inputs.area }}: {{ instruction }}"
  - id: review
    agent: codex
    prompt: "Review the change. The implementer said: {{ nodes.implement }}"
edges:
  - [implement, review]
loop:                             # defaults for `engine loop add`
  every: 6h
  instruction: Find and fix one flaky test.
```

Edges form a DAG: nodes nothing points at run first (after a workspace step
that checks the repository out), nodes that point nowhere end the run, and a
node with several predecessors waits for all of them. Prompts may use
`{{ instruction }}`, `{{ repository }}`, `{{ inputs.NAME }}` and
`{{ nodes.ID }}` (an upstream node's output). Registration rejects unknown
fields, unknown or reserved node ids, dangling or duplicate edges, cycles,
placeholders naming undeclared inputs or nodes that do not run first, and
runners the backend does not have — all problems at once, each with its path.

## Agents

```bash
engine agents
engine agent add claude --name reviewer --model opus
engine agent add opencode --name qwen --model qwen3-coder --url http://gpu.local:8000/v1
engine agent signin reviewer
engine agent remove qwen
```

A graph node's `agent:` names an agent on the backend. `claude`, `codex` and
`opencode` are built in; `agent add` offers one of those harnesses under a name
of its own, stored on the backend. `--model` is what it runs when a node names
no model, or a tier the backend has no entry for. `--url` points an opencode
agent at an OpenAI-compatible endpoint, which needs `--model` and no sign-in.
`agent signin` signs in the harness an agent runs on; `engine runner signin`
still works for one release.

## Runs

```bash
engine graph run fix-flaky-test "tests/test_slack.py flakes on CI" --wait
engine go review feat/my_feat -i adversarial=true
engine run get run-0123abcd --pretty
engine runs
engine runs --graph fix-flaky-test --status failed
```

`graph run` follows the run live when stdin and stdout are both TTYs. The view
shows stages, nodes, recent results and agent messages, and asks approvals inline
with y/n. Ctrl-C offers to detach or cancel; reattach to any run with
`engine run watch RUN_ID`. Finished runs print a summary; failure or cancellation
exits non-zero, while detaching exits 0.

Use `--no-watch` to return immediately on a TTY, or `--json` for JSON output.
Non-TTY invocations return immediately as before. `--wait` always polls without
interaction and exits non-zero if the run failed. `engine run get RUN_ID --pretty`
lists pending approval IDs, nodes and commands, plus the run's UI URL. Each submission carries an idempotency key
(`--idempotency-key` to choose it), and the client retries a dropped
connection with the same key, so a retry never starts a second run. A run
records status, node executions, each node's result, usage, pull requests
opened, and failure details. An agent without credentials fails with
`engine agent signin <agent>`; on a remote backend that command says what
to run on that host.

One built-in graph, `implementation-review-rerank`, serves every default verb.
It is based on `workflows/implementation_review_graph.py` and runs implementation,
CI, parallel review facets, reranking, one fix-and-review pass, and human review.

| Command | Inputs | Behavior |
| --- | --- | --- |
| `engine go "add a feature" --spec` | `spec=true` | Write a specification before implementation. |
| `engine go review '#839'` | `review_only=true`, `publish_review=true` | Review an existing change; `-i adversarial=true` adds refutation. |
| `engine go fix '#839'` | `fix_only=true` | Address unresolved review threads, check CI, and reply to addressed threads. |

Connected runs publish to the forge; disconnected runs keep findings in run
results and commits locally. Fixes remain on the target branch. Fixing fork PRs is rejected before checkout
to prevent pushing a same-named upstream branch. Disconnected
fixes can use cached threads via `-i review_threads=JSON`. Only comments marked
`OWNER`, `MEMBER`, or `COLLABORATOR` are actionable; comment text remains untrusted
review data. Fix runs refuse branches already held by a checkout. The workspace
provider permits borrowing only with explicit `borrow_existing=True` opt-in. Private repositories
require `engine connect github` before running disconnected.

`engine go [VERB] [TARGET] [INSTRUCTION...]` recognizes a project verb only in
first position with a resolvable or inferred target. Targets accept a full
GitHub PR URL, `'#839'`, `'OWNER/REPO#839'`, or a branch. A full URL supplies the
repository even outside a checkout. Quote `#` targets so the shell does not
treat them as comments. `engine go review` uses the current branch and its open
PR when available. Remaining words supply focus, for example
`engine go review '#839' focus on the migration`.

`engine go review the auth module for races` is an implementation instruction
when `the` is not a branch. A sole invalid target reports
`no such branch or PR; to give an instruction, quote it`.

Use `engine verbs` to inspect project bindings and
`engine verb set review my-review` to replace one. All three default bindings point to
`implementation-review-rerank`. A replacement must declare `instruction` for
`go`, or `pr_url` or `ref` for target verbs; the API and CLI reject incompatible
bindings. These contracts describe inputs, never internal graph stages.

For one release, `engine graph run` accepts deprecated aliases for the default
graph: `review` sets `review_only=true`, `adversarial-review` also sets
`adversarial=true`, `implement-review` uses the defaults, and
`spec-implement-review` sets `spec=true`. The old `--branch` and `--agent` flags remain supported there.


`engine runs` lists runs newest first, whether submitted or started by a loop,
filtered by `--graph`, `--loop` and `--status`, at most `--limit` (default 20),
in the backend's project unless `--project` or `--all-projects` says otherwise.

Runs go through the daemon's WorkOrder path, so they appear in the web UI and
are approved under its `[approvals]` policy.

## Loops

```bash
engine loop add fix-flaky-test --max-prs 5 --max-spend 20 --every 6h
engine loops list
engine loop pause fix-flaky-test --reason "release freeze"
engine loop resume fix-flaky-test --max-spend 40
```

- Instruction and cadence come from the graph's `loop` section, overridden by
  `--instruction` and `--every`. A loop with neither is refused; no schedule is
  invented. The first run starts now unless `--no-run-now`.
- A loop pins the version that was latest when it was added.
- Runs never overlap. A tick that comes due while a run is active waits for it,
  and every tick missed meanwhile collapses into that one. Each tick is
  recorded before its run starts, so a duplicate tick starts nothing.
- Limits are cumulative over the loop's lifetime and recomputed from durable
  events, so they survive restarts.
- `--max-prs` counts pull requests a run *opened*, not updates to existing ones.
  Reaching it pauses the loop; the run that opened the last one may finish.
- `--max-spend` is USD: agent usage reported by each node's ACP session (the
  agent's own session cost, or tokens priced at list rates when it reports
  none). It excludes CI, hosting, and subscription-billed usage, which agents
  report without a price — when any usage is unpriced, `spendEnforceable` is
  `false` and the cap is not presented as enforceable. The active run holds
  the remaining budget as its reservation and is cancelled the moment the
  loop's spend reaches the cap.
- A loop also pauses when a run fails because a runner needs signing in.
  `resume` is refused while a limit is still reached; pass a higher one.

## Nodes and steering

```bash
engine nodes list --run run-0123abcd
engine node steer 1a0055fa-... "Use the existing retry helper instead" --wait
engine node get 1a0055fa-... --pretty
```

Steering addresses one node attempt and is kept in order, once per
idempotency key. Its status moves `accepted` (queued for that attempt) →
`delivered` (sent to the agent as a turn) → `applied` (the agent finished that
turn) — never `applied` on queueing alone. It ends `rejected` when the backend
has no live session for the attempt, or `undelivered` when the attempt ended or
the daemon restarted first. Steering a finished attempt or a non-agent node is
refused. Steering is not a retry and not an approval decision.

Steering sent before an agent's first turn starts is delivered after that turn.

## API

Mounted at `/api/v1` on the daemon:

| | |
| --- | --- |
| `GET /graphs`, `POST /graphs`, `GET /graphs/{ref}` | register and discover |
| `GET /runs`, `POST /runs`, `GET /runs/{id}` | list, start and inspect |
| `GET /runs/{id}/nodes`, `GET /nodes/{id}`, `POST /nodes/{id}/steering` | node executions and steering |
| `GET /loops`, `POST /loops`, `GET /loops/{ref}`, `POST /loops/{ref}/pause`, `POST /loops/{ref}/resume` | loops |
| `GET /agents`, `POST /agents`, `GET /agents/{name}`, `DELETE /agents/{name}` | agents graphs can name |
| `POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/end` | `engine agent claude|codex|opencode`: a run whose implementation node is a terminal's CLI |
| `GET /backend` | runners available, and the execution engine |

The service's tables live in the graph database and are created by the
`migrations/sqlite_graph` Alembic history.
