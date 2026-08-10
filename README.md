# engine

An agent engine: it takes a request ("fix the flaky auth test"), provisions a
workspace, runs a coding agent in it, and publishes the result for review —
durably, across process restarts.

The first real capability is the **planner** — a foreman that decomposes a
request into tasks, dispatches workers to run them, and reports back. It has a
small web UI. Everything else is still scaffold; see [Status](#status).

```bash
uv sync && uv run engine-control-server   # → http://localhost:8000
```

No credentials needed to try it: with none present it runs a scripted demo that
plans, dispatches, and writes real files. `ant auth login` switches it to Claude.

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
| `packages/domain` | Ids, events, commands, plan, run state. Data only. | *(nothing)* |
| `packages/engine` | The decision functions. Pure. | `domain` |
| `packages/ports` | The six capability protocols. | `domain` |
| `packages/runtime` | Foreman, dispatcher, plugin registry. | `domain`, `engine`, `ports` |
| `packages/web` | The planner surface: HTTP, SSE, UI. | `domain`, `engine`, `ports`, `runtime` |
| `packages/adapters/*` | Concrete implementations. | `domain`, `engine`, `ports`, `runtime` |
| `apps/*` | Composition roots. | everything |

The core — `domain`, `engine`, `ports`, `runtime`, `web` — never names an
implementation. It does not know Anthropic, OpenAI, Temporal, GitHub, Buzz,
or Postgres exist.
`domain` and `engine` additionally have **no third-party dependencies at all**:
they install on a bare interpreter.

`web` is in that list deliberately. It is the surface a consumer embeds, so a
vendor import there would make the neutrality of every layer beneath it
decorative — see [Provider-agnostic by construction](#provider-agnostic-by-construction).

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

## The planner is an agent with different tools

A planner and a worker are the same kind of thing, running through the same
`AgentRunner` port. The only difference is what they are handed:

| | Planner (foreman) | Worker |
| --- | --- | --- |
| Tools | `set_goal`, `add_task`, `dispatch_task`, `await_tasks`, `list_tasks` | `list_files`, `read_file`, `write_file`, `report` |
| Can delegate | yes | **no** |
| Touches files | no | yes |

The worker's list has no `dispatch_task`, so a runaway delegation tree isn't
something the prompt discourages — it isn't expressible. Delegation is one level
deep by construction.

### The model proposes; the engine disposes

The planner never mutates the plan. It calls a tool; the tool becomes a domain
event; `engine.core.planning.decide_plan` folds it and decides what is legal:

```python
def decide_plan(plan: Plan, event: Event) -> PlanDecision:  # -> (Plan, commands)
```

A task can't be dispatched twice, can't start before its dependencies finish,
and can't be reopened once terminal. A confused planner therefore gets a refused
tool call it can read and recover from — not a corrupt plan:

```
dispatch_task(task_id="readme")
→ "readme is blocked on brief. Await those first."
```

None of those rules need an LLM, so none of them are left to one. Dispatch still
comes back as a `StartAttempt` command; `engine.runtime.Foreman` is what turns it
into a running worker.

## Provider-agnostic by construction

The planner runs on Claude, but nothing that ships names a vendor. Backends
are resolved **by name** from packaging entry points:

```toml
# in an adapter's pyproject.toml
[project.entry-points."engine.agent_runners"]
anthropic = "engine.adapters.anthropic:build_agent_runner"
```

```bash
ENGINE_AGENT_RUNNER=anthropic            # exactly this, fail if unusable
ENGINE_AGENT_RUNNER=anthropic,scripted   # first that works (the default)
ENGINE_AGENT_RUNNER=strands              # a backend we've never heard of
```

That last line is the point. A consumer installs their own adapter package and
selects it with one environment variable — no fork, no edit to our code. Neither
`engine-web` nor `engine-control-server` has a required dependency on any
adapter; they install without a vendor, and the convenience bundles are extras
(`uv sync --extra anthropic`).

Three runners ship today — `anthropic` (real), `scripted` (offline demo and
test double), and `openai` (placeholder). Adapters are named for the provider,
not the product: which agent a provider offers is a `model` choice, not a
separate adapter. The scripted one matters more than it looks:
if it and the Anthropic runner both drive the identical planner unchanged, the port
is genuinely neutral rather than neutral-shaped.

> **One honest caveat.** The registry loads adapters at runtime, which static
> analysis cannot see — so the AST boundary checks pass regardless of what it
> does. `test_registry_names_no_adapter` covers that gap directly by asserting no
> executable line in `registry.py` names a vendor.

## Capabilities

Six things the engine needs from the world. Each is a `Protocol` in
`packages/ports`, so an adapter satisfies it by shape alone — no base class to
inherit, no import required at runtime.

| Capability | Port | Intended first implementation |
| --- | --- | --- |
| Workflow Runtime | `WorkflowRuntime` | Temporal |
| Source Control | `SourceControl` | GitHub |
| Agent Runner | `AgentRunner` | Anthropic (live), OpenAI (placeholder) |
| Communications | `Communications` | Buzz |
| Workspace Provider | `WorkspaceProvider` | local git worktrees |
| State Store | `StateStore` | Postgres |

Ports are named for *what* is needed, never *who* provides it — `publish` and
`request_review`, not `open_pr`. Every command in `engine.domain.commands` is
fulfilled by exactly one capability.

## Layout

```text
packages/
  domain/                    engine.domain
  engine/                    engine.core
  ports/                     engine.ports
  runtime/                   engine.runtime
  web/                       engine.web
  adapters/
    anthropic/               engine.adapters.anthropic     (runner: anthropic)
    scripted/                engine.adapters.scripted      (runner: scripted)
    openai/                  engine.adapters.openai        (runner: openai)
    temporal/                engine.adapters.temporal
    github/                  engine.adapters.github
    communications/          engine.adapters.communications
    workspace/               engine.adapters.workspace
    postgres/                engine.adapters.postgres

apps/
  control_server/            engine.apps.control_server
  worker/                    engine.apps.worker
```

Every package publishes into the shared `engine.*` namespace (PEP 420), so the
import path mirrors the directory tree. The one exception is
`packages/engine`, which imports as `engine.core` — `engine.engine` would
read poorly.

`apps/` is where implementations get chosen — but the two apps choose
differently, and the difference is whose composition root it is. `worker` is a
deployable we operate, so it imports its infrastructure adapters directly.
`control_server` is shipped to consumers, so it imports none and resolves the
agent backend by name. Each app owns its own `composition.py`; they are kept
separate rather than shared because the processes are expected to diverge.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync            # install every workspace package, editable
uv run pytest      # run the suite, including the boundary checks
```

Run the planner UI:

```bash
uv run engine-control-server           # → http://localhost:8000
```

It reports which backend it resolved on startup. With no credentials that is
`scripted`, which plans and dispatches for real against a canned transcript —
the workers genuinely write files. To drive it with Claude, authenticate
(`ant auth login`, or export `ANTHROPIC_API_KEY`) and restart.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENGINE_AGENT_RUNNER` | `anthropic,scripted` | Backends to try, in order |
| `ENGINE_WORKSPACE` | `.engine-workspace` | Where workers read and write |
| `ENGINE_MODEL` | adapter's default | Model hint passed to the backend |
| `ENGINE_HOST` / `ENGINE_PORT` | `127.0.0.1` / `8000` | Bind address |

Force the offline demo on a machine that *does* have credentials with
`ENGINE_AGENT_RUNNER=scripted`.

## The boundaries are enforced, not just documented

`tests/test_boundaries.py` checks the diagram above by parsing the source tree
with `ast` — statically, so a violation fails the moment it is written, whether
or not any test executes that line. It catches deferred imports inside
functions, imports hidden behind `if TYPE_CHECKING:`, and relative-import
escapes like `from ..adapters.github import ...`.

The rules, one test each:

- `domain` and `engine` import nothing outside the standard library, and
  declare no third-party dependencies.
- No core package imports `engine.adapters.*` or `engine.apps.*` — and `web` is
  a core package, so the shipped surface is covered by that rule.
- Every package imports only from the layers permitted to it.
- Adapters do not import each other (two adapters that need each other belong
  behind one port).
- Only `apps/` depends on adapters, and apps do not depend on each other.
- `engine-web` and `engine-control-server` declare no *required* adapter
  dependency, so a consumer installs them without inheriting our vendor.
- Every app either imports an adapter or resolves one — an app that does
  neither means nothing anywhere chooses an implementation.
- No executable line in `registry.py` names a vendor.

`tests/test_layout_helpers.py` tests the checker itself, because a boundary test
that silently fails to parse an import reports green while the wall it guards
has a hole.

To add a package, create it under `packages/` or `apps/` with a `pyproject.toml`
and a `src/engine/...` tree; discovery in `tests/layout.py` picks it up, and
`_layer_for` decides which rules apply.

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

**Scaffolding** (Ticket 1) is complete: 15 packages, six capability ports, and
the dependency rules enforced by tests rather than documented.

**The planner** works end to end. A foreman decomposes a request, dispatches
workers in dependency order, and the workers do real work in a real workspace.
It runs on Claude when credentials resolve and on a scripted transcript when they
don't — the same planner code either way. The web UI streams planner text, tool
calls, worker output, and a live plan board.

Not yet implemented, by design — these adapters raise `NotImplementedError`
naming the ticket that fills them in:

- No Temporal client, worker, or workflow definition.
- No GitHub API calls and no git operations.
- No message delivery, no database, no schema, no migrations.
- No task-queue polling in the worker; the control server holds one planner
  session in memory, so a restart loses it.
- No OpenAI agent execution — the adapter registers and constructs, nothing more.

Two deliberate limits worth knowing about:

- **Workers have no shell.** They read, write, and list files inside a confined
  workspace root. A model-authored `run_command` needs an executable allowlist,
  argument rejection, timeouts, and real isolation — and the workspace provider
  that would supply the last of those is still a placeholder. Adding one before
  then would ship an unsandboxed shell driven by an LLM.
- **Filesystem confinement is path-based**, not kernel-enforced: every model-
  supplied path is resolved to canonical form and rejected if it escapes the
  root (`tests/test_planner.py` covers traversal, absolute paths, and the
  lookalike-sibling case). That is the right check, but it is a check — not a
  sandbox.

The domain vocabulary is a coherent placeholder chosen to make the boundaries
concrete; expect it to change when the engine's real state machine lands.

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