# Getting started with oe go

OpenEngine turns an instruction into a change, runs it through implementation
and review, and gives you commits or a pull request. Start with `oe go`
from a repository you already have checked out; you do not need to write a graph.
A **graph** is the sequence of steps that does the work. A **run** is one
execution of that graph, managed by a background **daemon**.

## 1. Install and first run (disconnected)

On macOS or Linux, install Git, Node.js 20.19+, and a supported coding CLI
(Codex, Claude, or OpenCode).
```sh
npm i -g @openengine.cc/engine
cd your-repo
oe --version
```

The first `engine` command installs a pinned OpenEngine release under
`~/.local/share/openengine` and starts the local daemon at
`http://127.0.0.1:4364`. (The alternative
[curl installer](https://openengine.cc/docs/) also puts an `engine` launcher in
`~/.local/bin`.) Check the service and available agents:

```sh
oe daemon status
oe daemon doctor
oe agents
```

Agents use the coding CLIs' logins on the daemon's machine. If needed, sign in
the agent you installed (choose one):

```sh
oe agent signin codex
oe agent signin claude
oe agent signin opencode
```

You only need one working coding agent. On the selected backend, `go` checks
its actual ACP adapter and configured model before starting the run. Automatic
selection uses only agents that can complete a short, tool-free readiness
request; the result is cached for up to a minute. This request uses the
provider's normal quota. Implementation and review
can use separate sessions of the same agent. Built-in reviewers inherit that
agent's configured or recommended model rather than requiring another model
or another vendor's login. OpenCode is also included when available.

An explicit `--agent` selection is never silently replaced. If its login or
model fails, the command reports that provider's error before starting work.
For Codex, OpenEngine prefers the installed `codex` binary, honors an explicit
`CODEX_PATH`, and uses the adapter's bundled version only when no installed
binary is available. Update the Codex installation on the backend if a model
is newer than the client it is running.

Open a terminal inside your project's Git checkout and give it a small task:

```sh
oe go "Add a regression test for empty input in the parser and fix the bug"
```

Without a forge connection, the run is **disconnected**: changes and findings
stay local. If GitHub is already connected, add `--local` to deliberately
try this mode:

```sh
oe go --local "Add a regression test for empty input in the parser and fix the bug"
```

When the run starts, a mode banner names the selected graph, mode, target, and
publication behavior. For example (graph and repository names vary):

```text
GRAPH · disconnected · /path/to/project · will keep results and commits local
```

In an interactive terminal, the live view shows the run ID, a stage bar, the
graph and node states, the current node and agent, and the latest messages and
tool activity. When approval is needed, answer `Allow? [y/n]` inline. The
[approvals section](#4-approvals) explains the install defaults.

Press **Ctrl-C**, then choose **d** to detach and leave the run running.
The same prompt also offers cancel or keep watching. Copy the run ID from the
view and reattach later:

```sh
oe run watch RUN_ID
```

Replace `RUN_ID` with that ID. A successful editing run leaves a **local branch
with commits**. Its final summary shows the workspace path, branch, results,
and a command to publish. Run that command when you want to share the change;
its form is:

```sh
git -C /path/from/run/summary push -u origin BRANCH_FROM_SUMMARY
```

Use the actual path and branch from the summary. This requires Git credentials
with push access to `origin`; pushing the branch alone does not open a PR.

## 2. Connect and work the normal way (connected)

For everyday work, connect GitHub on the selected daemon and follow the
authorization instructions it prints:

```sh
oe connect github
oe go "Add a regression test for empty input in the parser and fix the bug"
```

The banner now says `connected` and `will open a pull request`. OpenEngine
implements the change, opens a PR, and posts review findings on it. The final
summary links to the PR. The connection needs write access to the repository;
check the banner, since unverified access or `--local` keeps the run disconnected.

To review an existing change, use a PR URL, a quoted PR number in the current
repository, or a branch. These are alternatives:

```sh
oe go review https://github.com/OWNER/REPO/pull/123
oe go review '#123'
oe go review feature/parser-empty-input
```

`review` examines the change and reports findings. With a connected PR target,
it posts review comments on that PR; a branch without a PR keeps findings in
the run results. Quote `#123` so your shell does not treat it as a comment.

To address unresolved review threads on a PR:

```sh
oe go fix https://github.com/OWNER/REPO/pull/123
```

`fix` makes and pushes fixes to that PR's branch and replies to the review
threads. Replace the example URL or number with your own PR.

The word after `go` is a verb only when it is a configured verb and its target
resolves to a change. Multiword prose such as
`oe go review the auth module` remains an instruction when `the` is not a
resolvable target. An explicit PR target or a lone misspelled target produces
an error instead of silently becoming an instruction.

Inspect your project's bindings, or bind a verb to your own registered graph:

```sh
oe verbs
oe verb set review my-review
```

Here `my-review` must already be registered. The CLI checks its input contract:
`go` needs `instruction`; other verbs need `pr_url` or `ref`. See
[writing your own graph](#6-next-steps) below.

## 3. A shared daemon

A backend is a named daemon URL. To use a team daemon, have its administrator
give you the URL and bearer token. Set the token in an environment variable in
your shell, such as `ENGINE_TEAM_TOKEN`, then substitute your URL below:

```sh
oe backend add team https://engine.example.com --token-env ENGINE_TEAM_TOKEN
oe backends list --check
oe backend use team
```

The general form is `oe backend add NAME URL --token-env VAR`. The backend
configuration stores the variable's name, not its token. `oe backend use`
selects the default; `--backend NAME` selects one for an individual command:

```sh
oe go --backend team "Add a regression test for empty input in the parser and fix the bug"
oe run watch RUN_ID --backend team
oe run get RUN_ID --pretty --backend team
```

The shared daemon needs its own agent logins, forge connection, and access to
the repository. From a local checkout, the CLI identifies the remote repository
by `owner/repo` from `origin`; the daemon resolves it using its own `[repos]`
configuration. Local filesystem paths are not shared automatically. See the
[repository setup reference](https://github.com/OpenEngine/OpenEngine/blob/main/docs/repository-onboarding.md).

You can detach and reattach just as with a local daemon. `watch` follows live
and accepts approvals; `get --pretty` prints a snapshot of status and results.
The browser run page is `{backend}/runs/{run_id}`, for example
`https://engine.example.com/runs/RUN_ID`.

## 4. Approvals

The current install template sets `[approvals] auto_approve = false` and
`allow = ["read"]`: reads are automatically accepted; edits, shell commands,
and other tool permission requests ask for approval. Graph steps that ask for
a human decision can also pause.
Reinstalling preserves an existing configuration, so your settings may differ.

To change these grants, edit `[approvals]` in
`~/.config/openengine/engine.toml`: adjust `allow` and the shell rules, or set
`auto_approve = true` to automatically accept ordinary tool permission requests.
Explicit shell ask and deny rules still apply. Set `trusted = true` in
`[repos."owner/repo"]` to auto-approve WorkOrders for that repository; this
is not a shell-command allow list.
For details, consult the
[approval configuration reference](https://github.com/OpenEngine/OpenEngine/blob/main/packages/runtime/src/engine/runtime/config.py)
and [rule precedence](https://github.com/OpenEngine/OpenEngine/blob/main/packages/runtime/src/engine/runtime/approval_policy.py).

Restart the daemon after saving:

```sh
oe daemon stop
oe daemon start
```

For a shared backend, its administrator changes the configuration and restarts
the service on the daemon's machine.

## 5. Agents and models

Each agent a graph step can run, `codex`, `claude` and `opencode`, is
configured under `[runners.NAME]` in `engine.toml`: the binary it runs and the
model for each tier. Steps ask for a tier rather than a model: most run at
`default`, security review at `elevated`, and naming a run at `cheapest`.

```toml
[runners.codex]
path = "/usr/local/bin/codex"   # optional; default: CODEX_PATH, then codex on PATH
models = { cheapest = "", default = "", elevated = "" }

[runners.claude]
models = { cheapest = "haiku", default = "sonnet", elevated = "opus" }
```

A blank or missing tier leaves the agent to its own default; for Codex, the
`model` in `~/.codex/config.toml`. Steps read the tiers when they start, so
restarting the daemon after an edit is enough, including for runs of graphs
registered earlier. `oe agents` shows each agent's binary, its version and its
model per tier, marking those left to the agent's default.

## 6. Next steps

Write your own graph using the current
[`oe graph spec` reference](https://openengine.cc/docs/cli-specs/#engine-graph-spec),
then register your file:

```sh
oe graph spec
oe graph add my-review.yaml
```

For recurring work, register a maintenance graph and create a loop with limits:

```sh
oe loop spec
oe loop add my-maintenance --every 6h --instruction "Find and fix one flaky test" --max-spend 10 --max-prs 3
```

`my-maintenance` is your registered graph's name. The first run is due
immediately. `--max-prs` pauses after that many new PRs; `--max-spend` caps
reported USD usage and stops the active run at the cap. Unpriced subscription
usage cannot be enforced as a dollar budget. Limits accumulate across runs;
see the [`oe loop spec` reference](https://openengine.cc/docs/cli-specs/#engine-loop-spec)
for scheduling and lifecycle details.
