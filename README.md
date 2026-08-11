# engine

OpenEngine is your SDLC engine.
Changes -> Pool of Reviewers -> Reranking -> Impact Radius Analysis -> System Diagram -> Safe change 

## Getting started

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 20.19+.

```bash
uv sync            # install all 16 workspace packages, editable
npm --prefix apps/web install
npm --prefix apps/web run build
uv run pytest      # run the suite, including the boundary checks
```

All three entrypoints run today and report their wiring:

```bash
uv run engine-control-server
uv run engine-worker
uv run engine-web --check
```


# Pros
1. Not coupled to any provider (Anthropic, Codex, etc.)
2. Work from your phone on any project
3. Define workflows that achieve your review standards
4. Stringent security reviews and checklists
5. Augments your workflow, doesn't replace it 


An agent engine: it takes a request ("fix the flaky auth test"), provisions a
workspace, runs a coding agent in it, and publishes the result for review —
durably, across process restarts.

This repository is currently a **scaffold**. The package layout, the capability
boundaries, and the rules that keep them honest are in place; the behaviour
behind them is not. See [Status](#status).

## Dependency direction

```text
domain
  ↑
engine

ports
  ↑
runtime
  ↑
adapters

apps compose everything together
```

An arrow points from a package to what it is allowed to depend on: `engine`
depends on `domain`, `runtime` depends on `ports`, `adapters` depend on
`runtime`. Nothing points back down.

| Package | Role | May import |
| --- | --- | --- |
| `packages/domain` | Ids, events, commands, run state. Data only. | *(nothing)* |
| `packages/engine` | The decision function. Pure. | `domain` |
| `packages/ports` | The six capability protocols. | `domain` |
| `packages/runtime` | Dispatches commands to capabilities. | `domain`, `engine`, `ports` |
| `packages/adapters/*/*` | Concrete implementations, filed under the capability they implement. | `domain`, `engine`, `ports`, `runtime` |
| `apps/*` | Composition roots. | everything |

The core — `domain`, `engine`, `ports`, `runtime` — never names an
implementation. It does not know Temporal, GitHub, Codex, or Buzz exist.
`domain` and `engine` additionally have **no third-party dependencies at all**:
they install on a bare interpreter.

## The central rule: the engine emits commands

`engine.core.decide` is a synchronous, side-effect-free function:

```python
def decide(state: RunState, event: Event) -> Decision:  # -> (RunState, tuple[Command, ...])
```

It performs no I/O, reads no clock, and holds no reference to any adapter. When
work needs to happen in the outside world, it returns a `Command` saying so:

```python
next_state, commands = decide(state, RunRequested(...))
# commands == (ProvisionWorkspace(run_id=..., repository="acme/api", base_ref="main"),)
```

`engine.runtime.Dispatcher` is the only code that turns those commands into real
calls, against whichever implementations `apps/` wired up.

Three things fall out of that split, and they are the reason for it:

- **Testing needs no mocks.** Give `decide` a state and an event, inspect the
  commands. `tests/test_engine_emits_commands.py` does exactly this.
- **Replay is deterministic**, which is what a durable workflow runtime needs to
  resume a run after a crash.
- **Swapping infrastructure is an edit in one file** — the app's
  `composition.py` — because nothing below it names a vendor.

If you ever want to `import` an adapter from core, the thing you actually want
is a new command.

## Capabilities

Six things the engine needs from the world. Each is a `Protocol` in
`packages/ports`, so an adapter satisfies it by shape alone — no base class to
inherit, no import required at runtime.

| Capability | Port | First implementation | Adapter package |
| --- | --- | --- | --- |
| Workflow Runtime | `WorkflowRuntime` | Temporal | `adapters/workflow_runtime/temporal` |
| Source Control | `SourceControl` | GitHub | `adapters/source_control/github` |
| Agent Runner | `AgentRunner` | Codex, Claude Code | `adapters/agent_runner/codex`, `adapters/agent_runner/claude_code` |
| Communications | `Communications` | Buzz | `adapters/communications/buzz` |
| Workspace Provider | `WorkspaceProvider` | local git worktrees | `adapters/workspace_provider/git_worktree` |
| State Store | `StateStore` | Postgres, SQLite, in-memory | `adapters/state_store/postgres`, `adapters/state_store/sqlite`, `adapters/state_store/memory` |

Ports are named for *what* is needed, never *who* provides it — `publish` and
`request_review`, not `open_pr`. Every command in `engine.domain.commands` is
fulfilled by exactly one capability.

Adapters are filed the same way: `packages/adapters/<capability>/<vendor>`, so
the directory answers *what* and the leaf answers *who*. The Buzz adapter is
`communications/buzz`, not `communications` — a package named for the capability
silently claims all of it, and Slack has to be able to sit beside Buzz without
either one being the privileged implementation. The capability directory is
checked against the fields of `engine.runtime.Capabilities`, which is therefore
the list a seventh capability has to be added to first.

## Agent identity

An agent is described by data, not by a class, and the description is separate
from any execution of it. Three levels, in `engine.domain.agents`:

```python
AgentProfile(                       # the logical role. Configuration.
    agent_id=AgentId("foreman"),
    instructions="Coordinate implementation work, answer coder questions, ...",
    capabilities=("dispatch", "author_workflow"),
)
  ↓
AgentInstance                       # a durable instance, owning one conversation
  ↓
AgentRun                            # one execution of that instance
```

An instance outlives any single run, which is what lets an agent stop for
clarification and resume later as the same logical entity with the same history.
`capabilities` on a profile names the **tools** the agent is granted; the runtime
resolves each name to a concrete tool and a runner may offer the model nothing
outside that list, so a profile reads as the complete statement of what an agent
may do.

Conversations belong to the State Store, never to a model provider. An adapter
may keep a native session open for efficiency, but if the store is not the
source of truth then history cannot be resumed after a restart, inspected by a
human, or moved to another provider.

The `AgentRunner` port is **turn-shaped** rather than task-shaped:

```python
async def run_turn(agent_run_id, profile, messages, tools=(), workspace_id=None) -> AgentTurn
```

Tool use is a conversation, so the runner returns the tool calls the model asked
for and stops; executing them and deciding whether to go round again belongs to
the caller. One consequence is that chatting with the foreman and running a
headless coder are the *same call* with different profiles — a workspace is
optional context, not a mode.

The caller in practice is `engine.runtime.AgentSession`: it loads the
conversation, resolves the profile's grants to tools, runs the turn, and stores
both messages. Every chat surface goes through it, so the web API and future
ingress cannot drift into two different notions of what a conversation is. The
profiles themselves are values in `engine.runtime.profiles` — adding an
agent is adding an entry there, and nothing about it is special-cased anywhere
else. A profile never names its runner: `Capabilities.agent_runner` holds the
one a port is entitled to, and that is what anything non-interactive uses.

A process may additionally offer a *choice* of runner — `AgentSession` takes a
name-to-runner mapping, and the assistant-ui client turns it into a dropdown. The names
mean nothing below `apps/`, exactly like tool grants; binding "codex" and
"claude" to implementations happens in one function in the composition root.
Switching mid-conversation is allowed and is the point: we hold the transcript,
so whichever runner answers next is handed everything the other one said and
did.

## Layout

```text
packages/
  domain/                    engine.domain
  engine/                    engine.core
  ports/                     engine.ports
  runtime/                   engine.runtime
  adapters/
    workflow_runtime/
      temporal/              engine.adapters.workflow_runtime.temporal
    source_control/
      github/                engine.adapters.source_control.github
    agent_runner/
      codex/                 engine.adapters.agent_runner.codex
      claude_code/           engine.adapters.agent_runner.claude_code
    communications/
      buzz/                  engine.adapters.communications.buzz
    workspace_provider/
      git_worktree/          engine.adapters.workspace_provider.git_worktree
    state_store/
      postgres/              engine.adapters.state_store.postgres
      sqlite/                engine.adapters.state_store.sqlite
      memory/                engine.adapters.state_store.memory

apps/
  control_server/            engine.apps.control_server
  worker/                    engine.apps.worker
  web/                       engine.apps.web
```

Every package publishes into the shared `engine.*` namespace (PEP 420), so the
import path mirrors the directory tree. The one exception is
`packages/engine`, which imports as `engine.core` — `engine.engine` would
read poorly.

The capability directories under `adapters/` are namespace only: they hold no
`pyproject.toml` and no `__init__.py`. That is what lets a second vendor ship
`engine.adapters.communications.slack` from its own distribution, into the same
`engine.adapters.communications` namespace, without either package owning it.

`apps/` is the composition root, and the only layer permitted to name a
concrete adapter. Each app owns its own `composition.py`; the two are kept
separate rather than shared because the processes are expected to diverge.

### Chatting with an agent

Run these commands from the repository root. To build the UI and serve it with
the Python API:

```bash
npm --prefix apps/web install
npm --prefix apps/web run build
uv run engine-web
```

Then open <http://localhost:8000>.

For frontend development with live reload, run the API and Vite development
server in separate terminals:

```bash
# Terminal 1
uv run engine-web

# Terminal 2
npm --prefix apps/web run dev
```

Open <http://localhost:5173>; Vite proxies API requests to the Python server on
port 8000.

The assistant-ui client is the one part of the system that works end to end.
Pick an agent and a runner, open as many conversations as you need, and the
replies come from real models. A running conversation stays active when you
switch to another, so Codex and Claude turns can overlap across chats:

```text
you        ->  assistant-ui thread
               engine web API             stream and coordinate concurrent chats
               engine.runtime.AgentSession    load history, record the run
               engine.ports.AgentRunner       one turn
               engine.adapters.agent_runner.codex        codex exec --json
                 …or.claude_code                         claude -p            <- the model
               engine.adapters.state_store.sqlite   store the whole turn
```

It needs the [Codex CLI](https://developers.openai.com/codex/cli) or
[Claude Code](https://claude.com/claude-code) on `PATH` and logged in; the page
reports it plainly if the one you picked is missing. Both run read-only by
default — Codex sandboxed, Claude Code restricted to `Read`/`Glob`/`Grep` — so an
agent you are talking to cannot edit the tree as a side effect of answering.
Each new conversation gets its own branch-backed Git worktree under
`/tmp/engine-workspaces`, and the chat shows the `cd` command for opening that
checkout. The worktree is reused for every later turn in the conversation.

The engine transcript records what the agent *did*, not just what it concluded — the
commands it ran and their output are stored beside its messages, and the chat
page draws them as collapsible blocks. That is what lets a stateless runner stay
coherent: asked a follow-up, the agent reads the earlier command's output back
out of our transcript instead of re-running it.

It is also what makes the **Runner** dropdown more than a preference. Because the
conversation is ours rather than a provider's, either runner can pick up a
conversation the other started — including the other's tool output. Asked what
the previous assistant had run, Claude quoted the exact `find` command Codex
had used, having executed nothing itself.

Three limits worth knowing before you read anything into a conversation:

- **Conversations are local to this checkout.** SQLite stores them in
  `conversations.sqlite3` in the process working directory.
- **The agent has no engine tools.** Both CLIs bring their own (they read files,
  run commands) and neither can be handed ours, so the foreman can discuss
  dispatching work but not dispatch it. See `CodexToolsUnsupportedError` and
  `ClaudeToolsUnsupportedError`, which are raised rather than ignored.
- **Turns are expensive, and Codex turns are barely cached.** `codex exec` spends
  ~15k prompt tokens per model request on its own preamble and serves a flat ~10k
  of it from cache no matter what we send; Claude Code reaches ~86%. See the
  `TODO(caching)` block at the top of the Codex adapter for the measurements —
  the gap is `codex exec` rebuilding a process per turn, not something inherent
  to driving a CLI.

The other pages — Runs, Inbox, Request a run — are unwired and say so.

## The boundaries are enforced, not just documented

`tests/test_boundaries.py` checks the diagram above by parsing the source tree
with `ast` — statically, so a violation fails the moment it is written, whether
or not any test executes that line. It catches deferred imports inside
functions, imports hidden behind `if TYPE_CHECKING:`, and relative-import
escapes like `from ..adapters.source_control.github import ...`.

The rules, one test each:

- `domain` and `engine` import nothing outside the standard library, and
  declare no third-party dependencies.
- No core package imports `engine.adapters.*` or `engine.apps.*`.
- Every package imports only from the layers permitted to it.
- Every adapter sits under the capability it implements, named for its vendor
  rather than that capability, and its import path mirrors that directory.
- Every capability has at least one adapter filed under it.
- Adapters do not import each other (two adapters that need each other belong
  behind one port). Grouping by capability puts vendors side by side, which
  makes this the easy rule to break.
- Only `apps/` depends on adapters, and apps do not depend on each other.

`tests/test_layout_helpers.py` tests the checker itself, because a boundary test
that silently fails to parse an import reports green while the wall it guards
has a hole.

To add a package, create it under `packages/` or `apps/` with a `pyproject.toml`
and a `src/engine/...` tree; discovery in `tests/layout.py` picks it up, and
`_layer_for` decides which rules apply. A new adapter goes one level deeper, in
`packages/adapters/<capability>/`, and its distribution is named
`engine-adapter-<capability>-<vendor>` — the same order as the path.

## Continuous integration

`.github/workflows/tests.yml` runs on every push to `main`, every pull request,
and on demand. Two jobs, kept separate because they answer different questions:

**`dependency boundaries`** — did someone break the architecture? Runs the
boundary and checker tests on a single interpreter (they are static analysis, so
the Python version is irrelevant), and additionally:

- `uv lock --check`, so an edited dependency that was never relocked fails here
  rather than making every `--locked` install below test something other than
  what the pyprojects say.
- Installs `domain` and `engine` into an empty virtualenv, asserts that exactly
  two packages are present, and runs `decide` there. This is the strongest form
  of "no third-party dependencies" — not *we declared none* but *nothing else is
  installed and the core still works* — and it catches a dependency that arrives
  through packaging rather than through an `import`.

**`tests (py3.11 … py3.14)`** — did someone break the code? The full suite across
the whole range `requires-python` claims, with `fail-fast: false` so one
version's failure does not mask the others, then a smoke test that both
composition roots still start. `UV_PYTHON` is set at job level; without it the
`.python-version` pin (3.14) would win and all four legs would quietly test the
same interpreter.

To reproduce a CI leg locally:

```bash
uv lock --check
UV_PYTHON=3.11 uv sync --locked && UV_PYTHON=3.11 uv run pytest
```

## Status

Ticket 1 — scaffolding — is complete. In place:

- All 15 packages exist, install, and import.
- The six capabilities have ports, a `Capabilities` container covering every
  one, and at least one adapter each.
- `decide` handles one representative transition (`RunRequested` →
  `ProvisionWorkspace`) end to end, through the dispatcher, against a fake.
- The dependency rules are enforced by tests.

The agent vocabulary — profile, instance, run, conversation, tools — sits on top
of that, with `AgentRunner` reshaped around turns and `StateStore` given the
methods that make conversations ours.

**Chat works end to end.** Two of the six capabilities are real: two agent
runners drive the Codex and Claude Code CLIs and parse their event streams, and
the SQLite state store holds instances and conversations across restarts.
`engine.runtime.AgentSession` joins them, the assistant-ui client in `apps/web`
draws them, and two agents ship — `foreman` and `coder`, both just values in
`engine.runtime.profiles`. A conversation records the commands an agent ran and
their output, and either runner can continue one the other started. Different
conversations may run concurrently; turns within one conversation are
serialized so they cannot race against stale history.

Not yet implemented, by design — the remaining adapter methods raise
`NotImplementedError` naming the ticket that fills them in:

- No Temporal client, worker, or workflow definition.
- No GitHub API calls, no git operations.
- No message delivery, no database, no schema, no migrations.
- No HTTP surface on the control server, no task-queue polling in the worker.
- No engine tools, so no profile grants any: the foreman can discuss dispatching
  work but cannot dispatch it, and `AgentSession` refuses to run a profile whose
  grants resolve to nothing rather than quietly dropping them.
- No workspace-aware agent runs: the Codex adapter refuses a `WorkspaceId` it
  has no provider to resolve.

The run vocabulary (`events.py`, `commands.py`, `state.py`) is a coherent
placeholder chosen to make the boundaries concrete; expect it to change when the
engine's real state machine lands. `engine.domain.agents` and
`engine.domain.chat` are meant to be more durable — they describe identity, not
a state machine.

## Shape of the system

```
                   Workflow DSL
                  zero-dep Python
                       │
                       ▼
               ┌──────────────┐
Event + State →│    Engine    │→ Commands
               └──────────────┘
                       │
                       ▼
                    Runtime
                       │
             ┌─────────┼─────────┐
             ▼         ▼         ▼
          Temporal   Agents    Git/Buzz
```
