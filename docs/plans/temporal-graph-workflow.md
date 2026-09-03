# Run graph runtimes through Temporal

## Outcome

Every graph run is represented by a durable Temporal workflow execution. The
public `engine.graph_runtime.GraphRuntime` contract and its HTTP API remain
backend-neutral, while `engine.graph_runtime_langgraph` initially executes the
existing compiled LangGraph as one coarse-grained Temporal activity. That is a
deliberately naive first binding: Temporal owns run identity, lifecycle,
retries, cancellation, and command history, but LangGraph continues to own
supersteps and checkpoints. A later binding can replace the coarse activity
with the native LangGraph/Temporal integration without changing callers.

This is more than registering the current `GraphRunWorkflow` placeholder.
`TemporalService` presently registers workflow classes only at worker startup;
`LangGraphRuntime` launches `asyncio.Task`s in the API process and persists its
own run metadata; and `sqlite_runtime` owns the LangGraph checkpointer and run
store. Those three lifecycles need one composition boundary.

## Boundaries and names

Keep these concepts distinct:

- `engine.graph_runtime.GraphWorkflow` remains the small, structural protocol
  for a graph *definition* (`graph_id` and `name`). Renaming it would ripple
  through the catalog for no runtime benefit.
- Add a Temporal workflow implementation named `GraphRunWorkflow` (finishing
  the existing orchestrator placeholder). It represents one graph *execution*.
  In prose and Temporal metadata, call it the graph workflow; do not introduce
  a second Python type named `GraphWorkflow`.
- `GraphRuntime` remains the interface used by `create_app`. Add a Temporal-
  backed implementation/facade rather than importing `temporalio` into
  `engine-graph-runtime`.
- The orchestrator owns Temporal workflow policy and wire-safe command/result
  types. The LangGraph package owns the activity implementation because only it
  may compile and drive LangGraph.

This preserves the current package rule: the generic graph contract has no
Temporal or LangGraph dependency. `engine-orchestrator` may depend on the graph
contract, but it must accept concrete activities from the composition root
rather than import `engine.graph_runtime_langgraph`.

## Initial execution model

Use one Temporal workflow execution per Engine `RunId`, with a deterministic
Temporal workflow id such as `graph-run/{run_id}`. Starting the same run twice
must attach to or reject the existing execution instead of creating a second
LangGraph thread.

`GraphRunWorkflow.run` accepts a versioned, payload-only `GraphRunInput`
containing `run_id`, `graph_id`, and initial values. It invokes a named
`run_langgraph` activity. The activity opens the existing SQLite-backed runtime,
starts or reattaches to the LangGraph thread, drives it, and returns a
serializable terminal/interrupted result. Do not pass a compiled graph,
checkpointer, callback, ACP registry, exception, or `Path` through Temporal.

The workflow records only coordination state: current status, the latest public
snapshot, pending command ids, and the last published event sequence. LangGraph
remains authoritative for graph values and checkpoint history during this
phase. Temporal is authoritative for whether an execution was requested,
running, cancelled, or completed. Explicitly reconcile these stores when a
worker resumes: activity retries use the same `RunId`/thread id and must inspect
the LangGraph store before calling `start` again.

The coarse activity must heartbeat at superstep/event boundaries and include a
small resumption cursor. Set an explicit retry policy, heartbeat timeout, and a
long start-to-close timeout. Cancellation must stop the LangGraph driver and
close the runtime context before acknowledging cancellation.

### Control commands

Temporal must contain the durable command history, even though the first
LangGraph driver is coarse-grained. Define workflow updates (preferred, because
the caller receives validation/result) for:

- `steer(message, execution_id | node_id)`;
- `decide(approval_id, decision)`;
- `resume_from(checkpoint_id)`; and
- `cancel()`.

Queries expose the last workflow-owned snapshot and status. Updates validate
the run state in the workflow and assign an idempotency key. Each update then
executes a short `enqueue_graph_command` activity which writes and waits on a
durable SQLite command inbox alongside `graph-runs.sqlite3`; the long-running
graph activity polls that inbox and records the resulting snapshot when it
acknowledges the command. The short activity returns that durable result to the
update handler. This avoids trying to signal an activity (Temporal has no such
primitive) or performing SQLite I/O in deterministic workflow code. Do not use
a process-local `asyncio.Queue`: it loses accepted commands on worker restart
and makes Temporal history disagree with execution.

There is one important limitation to state and test: a blocked ACP permission
or steering point may need a command delivered while a node is still running,
not merely between supersteps. The inbox therefore has to be consumed by the
existing `NodeExecution` control path while the activity is alive. If that
cannot be done without embedding a Temporal client or nondurable queue in the
activity, ship the first slice as start/status/cancel only and keep the current
in-process runtime as the advertised control backend. Do not silently accept a
steer/approval update that cannot be delivered. Full live control is the gate
for making the Temporal facade the default HTTP runtime.

## Implementation slices

### 1. Make `TemporalService` a general worker host

Extend `TemporalService` with idempotent `register_activity` and
`register_activities`, expose registered activities for tests, and pass them to
`Worker`. Add a typed way to get/start a workflow handle by workflow id, since
the runtime facade needs to reconnect after process restart. Freeze
registration once `start()` begins (or restart the worker deliberately); the
current “registered for the next worker boot” behavior must not imply that a
late registration is active.

Finish `GraphRunWorkflow` with Temporal's workflow decorator, stable query and
update names, payload dataclasses, deterministic code only, and explicit
activity timeouts/retries. Register it in `Orchestrator.WORKFLOWS`; that tuple is
currently empty despite the exported placeholder classes.

Tests should cover workflow/activity registration, duplicate registration,
late registration behavior, handle reattachment, update validation, and worker
reboot retaining the same workflow execution.

### 2. Extract a reusable LangGraph run driver

Refactor `LangGraphRuntime` narrowly so its current in-process launch path and
a Temporal activity can share a `drive(run_id, graph_id, values, cursor)`
operation. Preserve the existing `GraphRuntime` contract and backend tests.
Make run-id allocation injectable or add `start_with_id`; Temporal must choose
the id before the activity starts, rather than accepting the random id currently
created inside `LangGraphRuntime.start`.

Move no Temporal types into this layer. The activity adapter translates the
wire DTOs to `GraphId`, `RunId`, snapshots, checkpoints, events, and failures.
Make store operations idempotent so an activity retry cannot publish a second
`run.started`, seed a second opening checkpoint, or launch concurrent drivers
for one thread.

### 3. Add the naive LangGraph activity and durable command bridge

In `engine-graph-runtime-langgraph`, add activity factories that close over
the deployment's graph definitions and state directory. A factory is preferable
to module globals: tests and multiple deployments need isolated catalogs and
stores. On invocation it enters `sqlite_runtime`, reattaches/starts the named
run, forwards runtime events to the heartbeat/result adapter, consumes durable
commands, and exits cleanly at a terminal state or cancellation.

The companion command activity inserts one idempotent command and waits for its
durable acknowledgement with heartbeats and bounded polling. The workflow
update awaits this activity and returns its result. The graph activity never
constructs a Temporal client or calls back into its own workflow.

Extend `SqliteGraphRuntimeStore` with a command inbox only if the live-control
spike proves it can feed `ExecutionRegistry` while nodes run. Persist command
id, workflow/run id, kind, payload, state (`pending`, `applied`, `rejected`), and
result/error. Claim and acknowledge transactionally; retries must replay the
recorded result rather than apply a command twice.

Add integration coverage using Temporal's time-skipping environment where
possible and the local service test for the real worker boundary. At minimum:
successful graph completion, activity retry after a simulated crash, process
reattachment, cancellation cleanup, duplicate start, resume from checkpoint,
and an approval or steering command delivered while a node is blocked.

### 4. Put a Temporal facade behind `GraphRuntime`

Add `TemporalLangGraphRuntime`, implementing `GraphRuntime` by talking to
`GraphRunWorkflow` handles. Static `graphs()`/`topology()` still come from the
local compiled definitions. Mutations become workflow starts/updates. Snapshot
reads use a workflow query for coordination plus the activity-owned durable
state where exact LangGraph history is required. Translate Temporal “not
found”, rejected update, cancellation, and failed workflow statuses into the
existing `GraphRuntimeError` family so the HTTP status contract does not
change.

Event delivery needs a durable cursor. Persist events in the existing event log
store (or add one beside the run store) before the workflow advances its
sequence. On facade restart, rebuild/replay `EventLog` from that cursor; do not
depend on callbacks held by the activity process. This keeps SSE reconnects and
`Last-Event-ID` working across the very restart Temporal is meant to survive.

Wire the composition root in this order: load `WorkflowCatalog`, create the
LangGraph activity from `catalog.graphs`, register the activity and
`GraphRunWorkflow` with `TemporalService`, start the service, construct the
facade, then pass it to `engine.graph_runtime.create_app`. The repository's
`implementation_review_graph.py` should become runnable without changing the
workflow definition itself.

### 5. Switch the default and prepare the native binding

Run the complete shared `GraphRuntime` contract suite against both the existing
in-process binding and the Temporal facade. Make the facade the deployment
default only after every operation—especially concurrent fan-out steering,
approval delivery, checkpoint forks, event ordering, and restart recovery—has
equivalent behavior.

Keep the coarse activity behind a small `GraphActivityDriver` protocol. The
future native LangGraph/Temporal plugin adapter should implement that boundary
or replace the activity factory, not alter `GraphRuntime`, HTTP routes, workflow
catalogs, ids, or command DTOs. Before adopting it, verify its exact guarantees
for checkpoint ownership, interrupts, parallel supersteps, retry idempotency,
and version compatibility; the current repository pins only broad LangGraph
minimums.

## Acceptance criteria

- A graph started through `GraphRuntime.start` has exactly one inspectable
  Temporal `GraphRunWorkflow` execution with the same Engine run id.
- Killing and restarting the worker resumes or reattaches without duplicate
  graph starts, checkpoints, commands, or events.
- The public graph HTTP API and all existing backend-neutral contract tests are
  unchanged.
- Workflow inputs, results, queries, and updates are versioned and serializable;
  Temporal workflow code performs no filesystem, SQLite, ACP, or LangGraph I/O.
- Activity cancellation closes LangGraph tasks, ACP sessions, checkpointers,
  and SQLite stores.
- Steering, approvals, and checkpoint resume either work durably through
  Temporal or are explicitly rejected and prevent the Temporal facade from
  becoming the default.
- The implementation-review graph loaded by `WorkflowCatalog.graphs` completes
  end to end through the naive activity.
- Replacing the naive driver with the native plugin requires no API or workflow
  definition changes.

## Risks to resolve early

The highest-risk item is live command delivery into an in-flight ACP-backed
LangGraph node. Prove that before broad refactoring. The second is dual
persistence: Temporal history and LangGraph SQLite can each say a retry won, so
all crossings need stable ids and idempotent writes. Finally, a long activity
can provide durable orchestration but not node-level Temporal recovery; until
the native integration replaces it, a worker loss resumes from the latest
LangGraph checkpoint and may repeat side effects performed after that
checkpoint. Document that at the operator surface and require graph nodes to be
idempotent in the interim.
