# engine

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
| `packages/adapters/*` | Concrete implementations. | `domain`, `engine`, `ports`, `runtime` |
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

| Capability | Port | Intended first implementation |
| --- | --- | --- |
| Workflow Runtime | `WorkflowRuntime` | Temporal |
| Source Control | `SourceControl` | GitHub |
| Agent Runner | `AgentRunner` | Codex |
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
  adapters/
    temporal/                engine.adapters.temporal
    github/                  engine.adapters.github
    codex/                   engine.adapters.codex
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

`apps/` is the composition root, and the only layer permitted to name a
concrete adapter. Each app owns its own `composition.py`; the two are kept
separate rather than shared because the processes are expected to diverge.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync            # install all 12 workspace packages, editable
uv run pytest      # run the suite, including the boundary checks
```

Both entrypoints run today and report their wiring:

```bash
uv run engine-control-server
uv run engine-worker
```

## The boundaries are enforced, not just documented

`tests/test_boundaries.py` checks the diagram above by parsing the source tree
with `ast` — statically, so a violation fails the moment it is written, whether
or not any test executes that line. It catches deferred imports inside
functions, imports hidden behind `if TYPE_CHECKING:`, and relative-import
escapes like `from ..adapters.github import ...`.

The rules, one test each:

- `domain` and `engine` import nothing outside the standard library, and
  declare no third-party dependencies.
- No core package imports `engine.adapters.*` or `engine.apps.*`.
- Every package imports only from the layers permitted to it.
- Adapters do not import each other (two adapters that need each other belong
  behind one port).
- Only `apps/` depends on adapters, and apps do not depend on each other.

`tests/test_layout_helpers.py` tests the checker itself, because a boundary test
that silently fails to parse an import reports green while the wall it guards
has a hole.

To add a package, create it under `packages/` or `apps/` with a `pyproject.toml`
and a `src/engine/...` tree; discovery in `tests/layout.py` picks it up, and
`_layer_for` decides which rules apply.

## Status

Ticket 1 — scaffolding — is complete. In place:

- All 12 packages exist, install, and import.
- The six capabilities have ports, placeholder adapters, and a `Capabilities`
  container covering every one.
- `decide` handles one representative transition (`RunRequested` →
  `ProvisionWorkspace`) end to end, through the dispatcher, against a fake.
- The dependency rules are enforced by tests.

Not yet implemented, by design — every adapter method raises
`NotImplementedError` naming the ticket that fills it in:

- No Temporal client, worker, or workflow definition.
- No GitHub API calls, no git operations, no agent execution.
- No message delivery, no database, no schema, no migrations.
- No HTTP surface on the control server, no task-queue polling in the worker.
- No web UI.

The domain vocabulary (`events.py`, `commands.py`, `state.py`) is a coherent
placeholder chosen to make the boundaries concrete; expect it to change when the
engine's real state machine lands.
