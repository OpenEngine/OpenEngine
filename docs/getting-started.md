# Getting started

OpenEngine runs a graph: a sequence of steps such as implementing a change,
checking CI, and reviewing it. The CLI sends requests to a **daemon**, also
called a **backend**, which runs the agents and owns their workspaces.
Use your team's remote daemon for work that ends in a reviewed pull request,
or your local daemon to try a starter graph inside a checkout.

These commands were checked against this checkout's CLI help and generated
graph/loop specifications (release version 1.6.0). The live terminal view
proposed in PR #836 is not present here: there is no `engine run watch`.

## Install

On macOS or Linux (including WSL on Windows), install Git and Node.js 20.19+
first, then run:

```sh
curl -LsSf https://openengine.cc/install.sh | sh
```

The installer puts `engine` in `~/.local/bin` and starts a local daemon at
<http://127.0.0.1:4364>. Add `~/.local/bin` to your shell's `PATH` if `engine`
is not found. Rerun the installer to upgrade. For local runs, agents also need
to be signed in on this machine; for remote runs, they sign in on the daemon's
host. You do not need local agent credentials just to submit remote work.

## Remote: submit a change and follow its PR

Ask the daemon operator for its URL, an API bearer token if required, and an
onboarded repository. The daemon needs agents signed in on its host, repository
access, and a forge connection that can push branches, open PRs, read CI, and
post comments. See [repository onboarding](repository-onboarding.md) and
[forge connections](../README.md#github-connection) for setup.

Export the supplied API token as `OE_TEAM_TOKEN` in your shell. Replace the
example URL below with the daemon URL (not the MCP gateway URL). `team` is a
name you choose locally; `--token-env` stores the variable name, not its value.

```sh
engine backend add team https://engine.example.com --token-env OE_TEAM_TOKEN
engine backends list --check
engine backend use team
engine agents --backend team
```

The selected backend is the default for subsequent commands; `--backend NAME`
overrides it for one command. Backend selection does not configure your MCP
client or change which website you have open.

### Start the work order

The shipped **Implementation review rerank** workflow
(`implementation-review-rerank`) implements the task, opens a PR, checks CI,
runs five reviewers in parallel, then reranks their findings to remove noise.
Surviving findings are posted as PR comments. It can make one automatic
fix-and-review pass, then produces impact analysis and waits for human review.

The simplest entry point is the remote web UI:

1. Open `{backend}/runs/new`, for example
   `https://engine.example.com/runs/new`, and sign in if required.
2. Select **Implementation review rerank** under **Workflow definition** and
   the intended **Repository**. If absent, have the operator configure the
   workflow or use **Add repository** to onboard the repository.
3. Keep the starting state at **Planning** and the forge mode **connected**.
   Disconnected mode does not publish a PR or comments. Choose signed-in
   implementation and review runners in **Workflow inputs**.
4. Enter a concrete **Task prompt**, such as “Fix the failing login test,
   preserve the existing API, and run the relevant tests.” Select
   **Create WorkOrder**. The run page opens immediately.

Alternatively, connect an MCP client using the [remote MCP guide](remote-mcp.md)
and call `create_workorder` with:

```json
{"prompt": "Fix the failing login test, preserve the existing API, and run the relevant tests."}
```

The gateway must be configured for your repository and
`implementation-review-rerank` workflow. The tool returns `{"run_id": "…"}`
and starts execution immediately; every call creates new work. Check the run
list before retrying after a lost response.

Slack mentions can also create work orders when the operator has connected
Slack and configured `work_orders.repository` and the workflow; see
[Slack integration](../site/docs/integrations/slack.md). For an **existing PR**,
requesting Engine as a reviewer on GitHub triggers parallel review, reranking,
and published findings/impact analysis when the webhook integration is set up.
That is a review entry point, not a request to implement a new task; see
[GitHub webhooks](github-webhooks.md).

> **CLI gap:** `engine graph run implementation-review-rerank` is not a
> work-order submission command. This Python workflow is offered by the
> daemon's work-order catalog, not automatically registered in the CLI graph
> registry, so that command reports “no graph named …” on a fresh install.
> Use the web UI or MCP path above for the full implementation/CI/review/PR flow.

### Follow the result

Replace `RUN_ID` with the ID from MCP or the run page:

```sh
engine run get RUN_ID --backend team --pretty
```

Follow the graph, agent conversations, and approval prompts at
`{backend}/runs/{run_id}`. Follow CI and posted review findings on the linked PR.
An approval or final human-review pause requires your response; it is not a
hung agent. The daemon operator's approval policy applies to remote work.

## Local: run a starter and inspect every step

Install the Claude Code and Codex CLIs if you have not already, then check
the local daemon independently of your selected remote backend:

```sh
engine daemon status
engine daemon doctor
engine agents --backend local
engine agent signin claude --backend local
engine agent signin codex --backend local
```

`engine agents` lists configured agents; it is not a login-status check.
Use `engine agent signin` to establish the required logins, and inspect a
run's `failure.authRequired` if authentication fails. Sign in only where
needed. The implementation starters use Claude by default
and a different runner for review, so have both Claude and Codex available.
Sign-in for a remote agent must happen on that backend's host; local sign-in
does not authenticate a remote daemon.

Open a terminal **inside an existing Git checkout** with a usable default
branch, then run this small documentation task:

```sh
engine graph run implement-review "Add a short repository overview in FIRST-RUN.md. Read the code first and leave the changes uncommitted." --backend local --pretty
```

Omit `--repo`: the CLI sends the checkout's local path to the local daemon.
The daemon creates a separate run workspace; edits appear there, not in your
original checkout. Copy the printed run ID for the commands below.

The built-in starters register themselves on their first run in a project:

| Starter | What it does |
| --- | --- |
| `review` | Parallel branch review and consolidated findings; changes no files. Supply the branch with `--input ref=YOUR_BRANCH`. |
| `adversarial-review` | Attacks a branch, then tries to refute each finding; changes no files. Supply `--branch YOUR_BRANCH`. |
| `implement-review` | Implements, reviews with a different agent, and fixes surviving findings once. |
| `spec-implement-review` | Writes a short spec before the implementation/review/fix cycle. |

The implementation starters leave changes **uncommitted**. They do not perform
the remote work-order workflow's CI/PR publication sequence.

### Follow and steer

```sh
engine run get RUN_ID --backend local --pretty
engine nodes list --run RUN_ID --backend local
engine node get EXECUTION_ID --backend local --pretty
engine node steer EXECUTION_ID "Keep the overview under 20 lines." --backend local --wait
```

Use an execution ID from the node list, not the graph's step name. Steering
sends an instruction to an active node; it does not approve a pending command
or restart a failed run. Repeat `run get` to see node results and failures.
For the workspace path and pending approval IDs, use JSON output:

```sh
engine run get RUN_ID --backend local
```

Read `workspace.path` to find the checkout containing the final uncommitted
changes. Review those changes before deciding what to keep. A successful run
reports `completed`; `failure` explains a failed run.

### Answer approvals

Fresh installs use this policy in `~/.config/openengine/engine.toml`:

```toml
[approvals]
auto_approve = false
allow = ["read"]
```

Expect the first non-read command or edit that requires permission to pause
the run. Open `http://127.0.0.1:4364/runs/RUN_ID` and accept or cancel the
request. JSON `run get` also lists `pendingApprovals`, including `approvalId`,
`reason`, and `command`; the current `--pretty` view does not print that list.

For API clients, replace `RUN_ID` and `APPROVAL_ID` below with those values:

```sh
curl --fail -X POST \
  http://127.0.0.1:4364/api/v1/runs/RUN_ID/approvals/APPROVAL_ID \
  -H 'Content-Type: application/json' \
  -d '{"decision":"accept"}'
```

Use `{"decision":"cancel"}` to refuse the request. For a protected remote
daemon, use its URL and add `-H "Authorization: Bearer ${OE_TEAM_TOKEN}"`.

In `[approvals]`, `allow` grants capabilities such as `read` or `edit` without
asking; `auto_approve = true` broadly approves requests. Optional
`[approvals.bash]` lists `allow`, `ask`, and `deny` command patterns. For shell
rules, deny wins over ask, which wins over allow; explicit patterns take
precedence over `auto_approve`. Choose policy on the daemon host, not the client.
After editing the local config, stop and start the daemon when no work is
running with `engine daemon stop` followed by `engine daemon start`.

## Next steps

Read the generated specifications and [CLI reference](../site/docs/cli-reference.md):

```sh
engine graph spec
engine loop spec
```

For your own graph, declare `inputs` and typed step `outputs`, reuse results
with `${outputs.STEP.field}`, select the same or a different agent with
`{same: STEP}` / `{not: STEP}`, and use `flow` for joins and conditional routing.
The [generated language reference](../site/docs/cli-specs.md) defines the fields.
Keep repository examples in [docs/examples/graphs/](examples/graphs/); the
existing [dead-code graph](examples/graphs/dead-code.yaml) demonstrates inputs,
outputs, agent selection, and loop defaults. Register your file with
`engine graph add PATH_TO_GRAPH.yaml --backend local`.

After running a registered graph successfully, schedule it from the checkout:

```sh
engine loop add YOUR_GRAPH --every 1d --instruction "Find one small improvement." --max-spend 20 --max-prs 1 --no-run-now --backend local
engine loop get YOUR_GRAPH --backend local --pretty
engine loop pause YOUR_GRAPH --reason "Finished experimenting" --backend local
```

Replace `YOUR_GRAPH` with its registered name. Without `--no-run-now`, the
first run starts immediately. Limits are cumulative; spend covers reported,
priced agent usage, so unpriced subscription usage cannot enforce a dollar
cap. Loops pin a graph version; registering a new version does not update an
existing loop. See the [loop specification](../site/docs/cli-specs.md#engine-loop-spec).

## Troubleshooting

- **Workspace fails with a raw Git error:** in 1.6.0, passing
  `--repo owner/repo` to a local starter requires a configured repository
  mapping. Run inside the checkout and omit `--repo`, as above.
- **“graph version … is not loaded”:** a starter registered before the
  `openengine.dev/v1` → `openengine.cc/v1` change can remain unloadable. The CLI
  only auto-registers a starter after a 404; registering corrected source under
  the same name can deduplicate to the stale version. Copy the current starter
  YAML from [the shipped sources](../cli/client/src/engine/cli/starters/), use
  `apiVersion: openengine.cc/v1`, and change its `name`, for example to
  `implement-review-fresh`. Save it as `implement-review-fresh.yaml`, register
  it with `engine graph add implement-review-fresh.yaml --backend local`, then
  use the new name in `engine graph run`. Existing runs keep their old version.
- **No progress:** check the run page or JSON `pendingApprovals` before retrying.
  For authentication failures, inspect `failure` and sign in the named agent
  on the daemon host. Use `engine daemon doctor` for local service problems.
