# Ongoing Projects on idle Resources

Status: proposed. This change delivers the specification only.

## Outcome and scope

Idle agent capacity should do useful work without anyone starting it by hand.
This RFC defines two things for that and scopes the WorkOrders that build them:

- A **Resource** is one agent and model pair that the engine can start work
  on. Example: `bazzite-qwen`, which is opencode driving Qwen through llama.cpp
  on the `bazzite` host.
- A **Project** is a standing prompt on one repository. It is started again
  whenever a Resource allowed to take it is idle. Example: "Use static analysis
  to find and delete unused code, or add tests for uncovered code." A project
  has a **max PRs** limit. When the limit is reached, the Resource moves on to
  the next project.

This starts Projects again from scratch. The current Project is a chat-born
container for Milestones, and it is hidden in this deployment
(`show_projects = false`). It is retired rather than extended (see WorkOrder 2).
Milestone scoping, pricing, budgets and preempting interactive work are out of
scope.

## What is installed today

opencode on the engine host (the mac mini):

- **Install:** opencode `1.18.30` at `/opt/homebrew/bin/opencode`. Its config
  is `~/.config/opencode/opencode.jsonc`, and it has the `@opencode-ai/plugin`
  `1.18.30` package.
- **Provider:** one, `bazzite` ("Bazzite (llama.cpp)"). It uses
  `@ai-sdk/openai-compatible` at
  `http://YOUR-BAZZITE.YOUR-TAILNET.ts.net:8000/v1`, reached over Tailscale.
- **Model:** one, `qwen3.8` ("Qwen 3.8 (27B Q4_K_M)"). It has
  `tool_call: true`, a 131072-token context and 16384 output tokens. The
  default model is `bazzite/qwen3.8`.
- **Reachability:** the endpoint answers from the engine host. It returns
  `401` without a key, so auth is enforced. `opencode models bazzite` lists
  `bazzite/qwen3.8`.
- **ACP:** `opencode acp` is built in and serves ACP over stdio. Unlike Codex
  and Claude, no `npx` adapter is needed.
- **Hygiene:** the provider's API key is currently stored in the config file.
  It should move to `"apiKey": "{env:BAZZITE_API_KEY}"` before a service
  account runs opencode unattended.

The engine's side:

- **Agents:** only `codex` and `claude` are registered. They are defined in
  `langgraph-acp/src/langgraph_acp/providers/`, bound by `build_runners()` in
  `apps/web/src/engine/apps/web/composition.py`, and listed in `AGENTS` and
  `RUNNER_CHOICES` in `workflows/implementation_review_graph.py`.
- **The review graph assumes exactly those two runners:**
  - `REVIEW_MODELS[runner]` fails for any other name.
  - `{"codex": "claude", "claude": "codex"}[runner]` picks the cross-reviewer,
    and it also fails for any other name.
- **Placement:** `choose_runners` (`packages/graph_runtime/src/engine/graph_runtime/inputs.py`)
  picks a runner for a single run from subscription utilization readings. A
  local model has no such readings, so it always counts as fully used. Nothing
  in the engine starts work because capacity is free.
- **Dispatch:** `dispatch_dependencies` in `apps/web/src/engine/apps/web/api.py`
  is the only background dispatcher. It starts SCHEDULED runs once their
  prerequisite has succeeded. There is no concurrency cap anywhere.
- **Placeholders:** `ProjectWorkflow` and `PacingWorkflow` in
  `packages/orchestrator` are empty classes.
- **PR ownership:** the graph database's `github_pull_requests` table records
  which run opened each pull request, including its `run_id`, `url` and
  `opened_at`. It does not record whether the pull request is still open.

## Model

### Resource

A Resource describes the deployment: which command runs, on which host, and
against which model. So it lives in `engine.toml`, not in the database:

```toml
[resources.bazzite-qwen]
runner = "opencode"          # a name bound in build_runners()
model = "bazzite/qwen3.8"    # passed as ACP session config; empty = runner default
concurrency = 1              # llama.cpp slots available to the engine
description = "opencode + Qwen 3.8 27B on bazzite"
```

- **Idle:** a Resource is idle when it has fewer than `concurrency` WorkOrders
  running on it. Runs started by hand with the same runner count against it
  too. `concurrency` defaults to 1, because a single llama.cpp server with one
  slot serializes requests anyway.
- **Liveness:** a later health probe of the llama.cpp `/health` or `/slots`
  endpoint can mark a Resource *unreachable*. This is optional (WorkOrder 7).

The runner is opencode through `opencode acp`:

```toml
[runners.opencode]
command = ["opencode", "acp"]
```

### Project

A Project is created and edited in the UI, so it lives in the state store:

| Field | Meaning |
| --- | --- |
| `project_id`, `name` | Identity. |
| `repository` | A key of `[repos]`. |
| `prompt` | The standing instruction given to every run. |
| `workflow` | Graph to run. Defaults to `[work_orders].workflow`. |
| `resources` | Names of the Resources allowed to take this project. Empty means any. |
| `max_open_prs` | How many of this project's PRs may be open at once. |
| `priority` | Order in the rotation. Ties break by the least recently served project. |
| `enabled` | Pause switch. A paused project is never started. |
| `cooldown_seconds` | Wait after a run that opened no PR (default 1 hour). |
| `created_by` | Who created the project. |
| `prompt_updated_by` | Who last set `prompt`. Starts as `created_by`. Runs are requested by this user. |
| `last_served_at` | When a Resource last started a run for this project. Nullable. Orders the rotation. |
| `consecutive_failures` | Runs that failed in a row. Reset by any run that succeeds. |
| `cooldown_until` | No run starts before this time. Nullable. Set when a run ends without a PR or fails. |

`run_states` gets a nullable `project_id` and `resource`, so each run knows the
project and Resource it was started for.

**Max PRs** counts this project's pull requests that are still open: runs with
this `project_id` that own a row in `github_pull_requests` whose pull request is
not yet merged or closed. At the cap, the project is not eligible and Resources
skip it. Merging or closing a PR frees a slot. This caps how much review work
the project creates, which is the scarce resource. Counting every PR ever
opened would leave projects stuck at the cap forever.

## Scheduling

The web app gets one background task, `dispatch_idle_resources`. It works like
`dispatch_dependencies`: it wakes on run completion, PR state change, project
edits and a 60-second tick. On each pass it does this for every Resource with a
free slot:

1. Skip the Resource if any interactive WorkOrder is waiting for it. Human work
   always goes first, and project runs are never preempted, only not started.
2. From the projects that are enabled, allowed on this Resource, under
   `max_open_prs` and out of cooldown, take the first by
   `(priority, last_served_at)`.
3. Start a WorkOrder on it through `start_graph_run`, with these settings:
   - **Workflow and runner:** the project's workflow, with this Resource's
     runner and model.
   - **Origin and requester:** origin `project:<id>`, and the requester (and
     commit co-author) is `prompt_updated_by`, the user who wrote the prompt
     being run. Editing someone else's prompt makes the editor answerable for
     the work it produces.
   - **Approvals:** the project approval policy below, never the deployment's
     `[approvals]`.
   - **Prompt:** the project prompt, followed by an engine-written preamble.
     The preamble lists the project's open PRs (titles, branches and changed
     paths) and says: "do not redo these; if nothing worthwhile remains, stop
     without opening a pull request".
4. Update `last_served_at`. The next free slot then takes the next project,
   which gives round-robin across projects of equal priority.

A run that ends without a PR, or fails, sets `cooldown_until` to now plus
`cooldown_seconds`. Without the cooldown, a project whose work is used up would
keep an idle Resource spinning forever. A failure increments
`consecutive_failures` and a success resets it. At two, the project is paused
(`enabled = false`) and a notice goes to the communications channel.

### Approvals for unattended runs

Nobody is watching a project run, so it cannot use the deployment's
`[approvals]`. `start_graph_run` turns on auto-approve for every node when
`[approvals] auto_approve = true`, which would give an unattended run blanket
approval. With it off, the run would wait on a permission request nobody
answers and hold the Resource's slot.

Project runs get their own policy, `[projects.approvals]`, with the same shape
as `[approvals]`, and `start_graph_run` takes the policy as a parameter:

```toml
[projects.approvals]
allow = ["read", "edit"]     # capabilities granted without asking

[projects.approvals.bash]
allow = ["uv run pytest **"] # explicit allowlist; `[approvals.bash].deny` also applies
```

- `auto_approve` is not accepted here. Config validation rejects it.
- Node-level auto-approve is never set for a project run.
- Any request the policy does not allow is **rejected**, not asked. The agent
  sees the refusal and can carry on or stop. A project run never waits on a
  human.

There is also a global switch, `[projects] enabled = true | false`, in
`engine.toml`.

## Review of project PRs

These runs use the existing `implementation-review-rerank` graph. The
implementation runner is the Resource. The review runner is also the Resource,
so idle work does not spend paid subscription tokens. A project can set
`inputs` to name another reviewer. The final human review happens on the PR
itself.

To make this work, the graph must stop assuming exactly two runners (WorkOrder
1). `REVIEW_MODELS` becomes optional per runner, so a runner that isn't listed
uses its default model. The cross-reviewer becomes a declared input that
defaults to the implementation runner.

## WorkOrders

Each item below is one PR of about 1000 lines or fewer, green on its own. They
are listed in dependency order, and 1 and 2 can run in parallel.

1. **opencode runner.**
   - Add `OpenCodeACPProvider(name="opencode", command=["opencode", "acp"])`
     in `langgraph-acp/providers/`, an `opencode_acp_runner` factory, and a
     `build_runners()` entry with a configurable command.
   - Register it in `AGENTS` and `RUNNER_CHOICES`, and generalize
     `REVIEW_MODELS` and the cross-reviewer mapping as described above.
   - Check against the installed `1.18.30` that these work: the ACP handshake,
     the `model` session config option, MCP server passing (for
     `git_subcommand` and `open_pull_request`), permission requests reaching
     `answer_permission`, and usage reporting. Record any gaps in the provider
     docstring.
   - Acceptance: a WorkOrder started by hand with
     `implementation_runner=opencode` opens a PR on a scratch repository.
2. **Retire the milestone Project.**
   - Remove the planning hierarchy: `Milestone`, `planning_tools.py`, the
     `/api/projects/.../milestones` routes and `scope_milestone`, the
     conversation-owned project, the Projects accordion and `show_projects`.
   - Include one Alembic revision in `migrations/sqlite` that drops
     `milestones` and `run_states.milestone_id`, and clears `projects` for the
     new shape.
   - Existing projects are hidden in this deployment and are not migrated.
3. **Resources.**
   - Add `[resources.*]` and `[runners.opencode]` to `EngineConfig`, with
     validation for unknown runners and `concurrency >= 1`.
   - Add a `resource` column on `run_states` (Alembic revision), and
     `GET /api/resources`, which returns each Resource with its running count,
     free slots and current run.
   - Nothing is scheduled yet.
4. **Projects store and API.**
   - Add the new domain `Project`, `project_id` on `run_states`, and store
     methods on all three state-store adapters (memory, SQLite, Postgres) plus
     the Alembic revision.
   - Add `GET/POST/PATCH/DELETE /api/projects`, and a `POST
     /api/projects/{id}/run` that starts one run on a chosen Resource by hand.
     This proves the whole path before any automation.
   - `POST` and `PATCH` check that the caller may use the `repository` and
     every named Resource, and reject the request otherwise. A `PATCH` that
     changes `prompt` sets `prompt_updated_by` to the caller.
5. **Open PR accounting.**
   - Add `state` and `closed_at` to `github_pull_requests` in
     `migrations/sqlite_graph`.
   - Keep them current from the existing GitHub webhooks, plus a slow
     reconciliation poll for missed deliveries.
   - Add a count of open PRs per project.
6. **Idle dispatcher.**
   - Add `dispatch_idle_resources` with the selection rules, cooldowns,
     failure pause, global switch, preamble and communications notice above.
   - Add `[projects.approvals]` and pass it to `start_graph_run` for project
     runs.
   - Tests use the in-memory store and a fake runner. They must cover the
     max-PR cap, rotation, cooldown, interactive work going first, and two
     wakeups racing without taking the same slot twice.
   - Approval tests: with `[approvals] auto_approve = true`, a project run has
     no auto-approved nodes; a request outside `[projects.approvals]` is
     rejected rather than left pending; an allowlisted bash command runs.
   - A test that the requester and co-author follow `prompt_updated_by`.
7. **Projects UI and Resource health.**
   - Add a Projects page (list, create/edit, pause, open PR count out of the
     max) and a Resources panel (busy/idle/unreachable).
   - Add an optional llama.cpp health probe, so an unreachable Resource is
     skipped instead of failing runs.

Once this RFC is accepted, each item can be filed as a WorkOrder under its
heading.

## Open questions

- **Should max PRs count open PRs or PRs per visit?** This RFC counts open
  PRs, which limits the review backlog. The alternative is "N PRs, then rotate
  to the next project even if they are still open". That can be added later as
  `prs_per_visit` if rotation should be forced.
- **Can a Project span several repositories?** This RFC allows one repository
  per Project. Several repositories would need the PR cap defined per
  repository.
- **Should opencode run on the engine host, or on bazzite itself?** Running it
  on bazzite, next to the model, would need a remote workspace, and today's
  workspaces are local git worktrees. This RFC keeps opencode on the engine
  host and only the model on bazzite.
