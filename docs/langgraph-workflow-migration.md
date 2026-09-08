# LangGraph workflow migration

Status: proposed

This plan replaces OpenEngine's workflow DSL and reducer with repository-owned
LangGraph graphs while preserving both supported deployment modes:

- ephemeral local: `MemoryStateStore` plus LangGraph `InMemorySaver`;
- durable local: SQLite application data plus `AsyncSqliteSaver`;
- scaled: Postgres application data plus Temporal's LangGraph integration.

The workflow run remains a first-class OpenEngine product object. LangGraph or
Temporal owns execution state; the run object owns identity, discovery,
authorization, user-facing metadata, and a repairable lifecycle projection.
There must be exactly one source of execution truth in each deployment.

## Goals

- Let workflow authors define a normal LangGraph `StateGraph` behind a small
  `@Workflow(...)` registration API.
- Supply reusable OpenEngine nodes for agent execution, workspace management,
  human review, publishing, and review reranking.
- Resume durable local runs after a process or machine restart.
- Execute the same graph topology locally and through Temporal.
- Preserve the current run, conversation, approval, planning, and UI concepts.
- Remove the custom transition interpreter and its serialized definition once
  legacy runs have drained.

This migration does not replace the application state store with LangGraph's
cross-thread `Store`, move conversations into graph checkpoints, or use a
Postgres LangGraph checkpointer beside Temporal.

## Current findings

The existing SQLite adapter has two kinds of data in one database.

Execution-specific data is confined to:

- `run_states`, whose `state_json` contains the reducer state and a complete
  `WorkflowDefinition` snapshot; and
- `run_events`, whose `event_json` is folded by the interpreter and also acts
  as an audit trail.

All other tables are application data and remain useful independently of the
interpreter:

| Current table | Disposition | Reason |
| --- | --- | --- |
| `projects` | Keep | Product planning data. |
| `milestones` | Keep | Product planning data. |
| `workstreams` | Keep | Product planning data and run grouping. |
| `agent_instances` | Keep | Durable agent/conversation identity. `workflow_step_id` initially stores the LangGraph node ID. |
| `messages` | Keep | Complete inspectable conversation history should not inflate every checkpoint. |
| `agent_runs` | Keep | Durable provider execution outcome and changed-file data. |
| `approvals` | Keep | Consent and audit records must outlive a graph invocation. |
| `session_grants` | Keep | Reusable consent is application state. |
| `run_states` | Replace | It duplicates the state LangGraph checkpoints will own and embeds the old DSL definition. |
| `run_events` | Replace | Reducer inputs disappear; retain a domain audit log with an idempotent, runtime-neutral schema. |

The current read model depends on data from both halves. It obtains run and
step status from `RunState`, graph labels and ordering from the snapshotted
`WorkflowDefinition`, and conversation/approval status from the application
tables. The replacement therefore needs both a lean graph state and a stable
workflow manifest; checkpoints alone are not an adequate list/query model.

## Target ownership model

```text
Workflow class + StateGraph + predefined nodes
                     |
          WorkflowCatalog / manifest
                     |
        +------------+-------------+
        |                          |
 LocalGraphRuntime          TemporalGraphRuntime
 InMemory/SQLite saver      Temporal LangGraph plugin
        |                          |
        +------------+-------------+
                     |
       WorkflowRunRepository + product store
          SQLite locally / Postgres at scale
```

The ownership rules are:

1. `WorkflowRun` is authoritative for stable identity and submission metadata.
2. The latest LangGraph checkpoint is authoritative for local execution state.
3. Temporal history is authoritative for scaled execution state. An
   `InMemorySaver` is used only inside the Temporal Workflow when LangGraph
   interrupts need a checkpointer.
4. Lifecycle and step fields on `WorkflowRun` are projections for list and UI
   queries. They never decide which node executes next.
5. Conversations, approvals, agent executions, planning records, and other
   cross-run data stay in the OpenEngine application store.

Projection updates and checkpoints cannot be committed in one transaction in
both runtimes. Runtime boundary hooks therefore upsert projections with stable
idempotency keys. On startup, and when an active run is opened, the runtime
compares the projection with the execution source and repairs any gap. Node
side effects must likewise be idempotent because a crash can occur after the
effect but before the next checkpoint, and Temporal Activities are retried.

## Public workflow API

The public surface should register a class and return an ordinary LangGraph
builder. Capitalizing `Workflow` makes it clear that the decorator produces a
registered workflow type rather than executing one.

```python
import openengine as oe
from langgraph.graph import END, START, StateGraph


class ImplementationState(oe.WorkflowState):
    review_findings: list[oe.ReviewFinding]
    critical_findings: list[oe.ReviewFinding]
    pr_url: str | None


@oe.Workflow(
    id="implementation-review",
    name="Implementation review",
    version="2",
)
class ImplementationWorkflow:
    @staticmethod
    def graph() -> StateGraph:
        implementation = oe.nodes.run_agent(
            profile=implementation_agent,
            workspace_access="write",
            editable=True,
        )
        review = oe.nodes.run_agent(
            profile=review_agent,
            workspace_access="read",
        )
        rerank = oe.nodes.rerank_review(max_comments=3)
        human_review = oe.nodes.human_review(
            title="Review proposed changes",
        )

        graph = StateGraph(
            ImplementationState,
            context_schema=oe.WorkflowContext,
        )
        graph.add_node("provision", oe.nodes.provision_workspace())
        graph.add_node("implementation", implementation)
        graph.add_node("review", review)
        graph.add_node("rerank", rerank)
        graph.add_node("human-review", human_review)
        graph.add_node("publish", oe.nodes.publish_changes())
        graph.add_edge(START, "provision")
        graph.add_edge("provision", "implementation")
        graph.add_edge("implementation", "review")
        graph.add_edge("review", "rerank")
        graph.add_edge("rerank", "human-review")
        graph.add_conditional_edges(
            "human-review",
            oe.routes.human_decision,
            {"approved": "publish", "changes_requested": "implementation"},
        )
        graph.add_edge("publish", END)
        return graph
```

`WorkflowCatalog` imports each module, discovers decorated classes, calls
`graph()` once at startup, and validates the resulting builder. The registration
key is `(id, version)`; runtime plugin names use a collision-free form such as
`implementation-review@2`. A version is immutable after release.

The decorator records metadata and registration only. It does not interpret
edges, retain user callbacks outside the LangGraph graph, or define another
workflow language.

### Predefined node contract

Each `oe.nodes` callable carries machine-readable metadata used during catalog
finalization:

- stable kind and display information for the UI manifest;
- whether it performs I/O and must execute as a Temporal Activity;
- timeout and retry defaults;
- required capabilities and workspace access;
- interrupt and resume payload schemas; and
- an idempotency-key strategy for external effects.

Catalog finalization copies the execution metadata onto the LangGraph node.
This meets Temporal's requirement that every node declare
`execute_in="activity"` or `execute_in="workflow"` without forcing local-only
authors to repeat Temporal configuration. Custom nodes are allowed only through
an `oe.node(...)` annotation that supplies the same contract. Conditional edge
functions must be pure and async so they are valid during Temporal replay.

`WorkflowContext` contains only serializable identifiers and configuration,
such as `run_id`, selected runner, and tenant/project IDs. Live database
connections, clients, and capability objects are resolved by an Activity/local
node service registry and are never checkpointed or sent through Temporal.

## Workflow run and graph state

Split the current `RunState` into two models.

`WorkflowRun` remains application data:

```text
run_id
workflow_id, workflow_version, graph_revision
task_id, workstream_id
name, prompt, repository
runner_name, workspace_id
execution_backend
status, current_node_id, terminal_outcome, failure_reason
manifest_json
created_at, updated_at
```

The identity, workflow version, input, backend, and manifest are authoritative.
The lifecycle fields are explicitly marked as projections. `graph_revision` is
a deterministic digest of the topology, state schema version, node contracts,
and routing contracts; it detects accidental changes to an immutable version.

The manifest is not an executable graph. It snapshots node IDs, order/display
groups, labels, kinds, editability, and public output schemas so the UI can
render old runs without importing old prompt text or reconstructing edges from
checkpoints.

The common checkpoint state contains only data needed to continue execution:

```text
run_id, task_id
workspace_id
status and current logical node when needed by nodes
node results and declared outputs
pending interrupt payload
retry/loop counters used to derive idempotency keys
failure and terminal result
```

Individual workflow states extend that schema. Agent transcripts, approvals,
large diffs, repository files, clients, and the graph definition stay outside
it. SQLite's saver stores full serialized checkpoint snapshots, so keeping
large artifacts out of state is important for database growth.

## SQLite schema

Durable local mode uses one SQLite file for straightforward backup and portable
installation, but with separate table ownership and separate connections:

- `SQLiteStateStore` owns application tables;
- `AsyncSqliteSaver` owns its checkpoint tables; and
- neither implementation queries or adds foreign keys into the other's tables.

Both connections use WAL mode, a configured busy timeout, and an application
startup/shutdown lifetime. The checkpointer must be closed cleanly. Ephemeral
tests use `MemoryStateStore` plus `InMemorySaver`; they do not try to share two
independent `:memory:` SQLite connections.

Add application-owned tables equivalent to:

```sql
CREATE TABLE workflow_runs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    graph_revision TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workstream_id TEXT REFERENCES workstreams(workstream_id),
    name TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    repository TEXT NOT NULL,
    runner_name TEXT NOT NULL DEFAULT '',
    workspace_id TEXT,
    execution_backend TEXT NOT NULL,
    status TEXT NOT NULL,
    current_node_id TEXT,
    terminal_outcome TEXT,
    failure_reason TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX workflow_runs_by_workstream
    ON workflow_runs (workstream_id, sequence DESC);

CREATE INDEX workflow_runs_by_status
    ON workflow_runs (status, sequence DESC);

CREATE TABLE workflow_run_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
    event_type TEXT NOT NULL,
    node_id TEXT,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX workflow_run_events_by_run
    ON workflow_run_events (run_id, sequence);
```

Do not copy the saver schema into an OpenEngine migration. At the time of this
plan, `AsyncSqliteSaver` creates and owns `checkpoints` and `writes`, keyed by
`thread_id`, `checkpoint_ns`, and `checkpoint_id`. OpenEngine sets
`thread_id=str(run_id)` and treats those tables as an upstream implementation
detail. Pin compatible `langgraph` and `langgraph-checkpoint-sqlite` versions,
let `AsyncSqliteSaver.setup()` manage its schema, and test upgrades against a
copy of a real database.

Use strict msgpack deserialization (an explicit allowlist is preferable) and
decide whether local checkpoint payloads require encryption at rest before the
feature leaves preview. Define a retention policy because checkpoints and
pending writes otherwise grow without bound.

### Field migration from `run_states`

| Current `RunState` field | Target |
| --- | --- |
| `run_id`, `task_id`, `workflow_id`, `workstream_id` | Authoritative `workflow_runs` columns and minimal graph identifiers. |
| `repository`, `prompt`, `name`, `runner_name`, `workspace_id` | `workflow_runs`; include references in graph state only when a node needs them. |
| `phase`, `current_step_id`, `failure_reason` | Graph/Temporal execution truth plus repairable run projection. |
| `agent_runs`, `current_agent_run_id` | Graph references plus existing `agent_runs`/`agent_instances` rows. |
| `agent_paused` | LangGraph pending interrupt; projected as action-required/waiting. |
| `step_results`, `human_review`, `human_reviews` | Typed graph state plus domain `workflow_run_events` for audit. |
| `max_agent_runs` | Workflow state/configuration, not the product run row. |
| `workflow_definition` | Remove. Replace with `(workflow_id, version, graph_revision)`, a versioned graph registry, and `manifest_json`. |

## Runtime behavior

### Ephemeral local

Compile the selected graph with `InMemorySaver` and use the in-memory
application store. This mode is for unit tests and disposable sessions only;
it makes no restart guarantee.

### Durable local

Compile the selected graph with a process-lifetime `AsyncSqliteSaver` and call
it with:

```python
config = {"configurable": {"thread_id": str(run.run_id)}}
await graph.ainvoke(initial_state, config=config, context=context)
```

Human-review and editable-agent pauses use LangGraph `interrupt()`. The API
reads the pending interrupt from `aget_state(config)` and resumes with
`Command(resume=...)`. Startup lists nonterminal `workflow_runs`, resolves each
exact graph version, inspects its latest checkpoint, repairs the projection,
and schedules runs whose snapshot has runnable next nodes. An interrupt remains
paused across reboot without an in-process task.

The local runtime owns process task tracking, cancellation, and startup
recovery. It must not infer activity solely from an `asyncio.Task`; the durable
snapshot is authoritative.

### Temporal and Postgres

The same finalized builders are registered with Temporal's `LangGraphPlugin`.
I/O, agent, database, filesystem, publishing, and interrupting nodes execute as
Activities. Pure state transformations and async routing execute in the
Temporal Workflow. Temporal owns retries, timeouts, timers, replay, worker
distribution, and execution history.

When interrupts require a LangGraph checkpointer, compile with
`InMemorySaver`. Temporal persists and reconstructs that state during replay;
do not add `AsyncPostgresSaver` or `AsyncSqliteSaver` to this path. The UI reads
pending review through a Temporal query and resumes it through a signal carrying
the `Command(resume=...)` value. Postgres stores the same `WorkflowRun` and
product records as local SQLite.

The existing Temporal adapter is a placeholder and has no `temporalio`
dependency. The implementation slice must add `temporalio[langgraph]>=1.27`,
pin a tested range, and isolate it behind `TemporalGraphRuntime` because the
integration is currently Public Preview.

## Versioning and reboot safety

Unlike the current serialized `WorkflowDefinition`, a LangGraph checkpoint
does not serialize executable topology. Reboot safety therefore requires an
explicit graph-version policy:

1. Every run stores `workflow_id`, immutable `version`, and `graph_revision`.
2. The catalog rejects duplicate IDs/versions and revision drift at startup.
3. A deployed build retains every graph version needed by a nonterminal run.
4. Startup fails that run with an actionable compatibility error, rather than
   silently using a newer graph, when its exact version is unavailable.
5. Changing state shape, node IDs, interrupt order, or routing requires a new
   version unless a tested checkpoint migration is supplied.
6. Temporal deployments also follow Temporal's worker-versioning and replay
   compatibility rules.

Completed runs need only their manifest and product records. Their executable
graph version may be removed after the retention window once no active run or
supported replay depends on it.

## Delivery plan

### 1. Prove dependency and runtime compatibility

- Pin LangGraph, SQLite checkpointer, and Temporal plugin versions.
- Build a small graph with one Activity-style node, one conditional route, and
  one interrupt.
- Run it under `InMemorySaver`, `AsyncSqliteSaver`, and the Temporal plugin.
- Verify Python 3.11, serializer allowlisting, cancellation, Activity retry,
  and clean connection shutdown.

Done means the same topology passes a runtime conformance test in all three
configurations and the Public Preview risk is accepted explicitly.

### 2. Add the workflow registration API

- Add `@Workflow`, `WorkflowState`, `WorkflowContext`, and the versioned graph
  catalog.
- Add predefined nodes and the custom-node metadata contract.
- Generate and snapshot the UI manifest during catalog loading.
- Validate stable node IDs, reachability, serializable state/context, Activity
  placement, async conditional routes, output schemas, and graph revision.

Done means `ImplementationWorkflow.graph()` is an ordinary `StateGraph` and
invalid graphs fail during startup with actionable errors.

### 3. Separate product runs from execution state

- Introduce `WorkflowRun` and `WorkflowRunRepository` operations instead of
  exposing reducer `load/save/append_events/history` on the broad `StateStore`.
- Add `workflow_runs` and `workflow_run_events` to SQLite, memory, and Postgres
  adapters.
- Rebuild `RunReader` from the manifest, run projection, conversations,
  approvals, and node outputs rather than `WorkflowDefinition`.
- Add idempotent projection/audit hooks and reconciliation.

Done means the existing runs UI and APIs can be served without reading
`run_states` for a new-format run.

### 4. Implement the local graph runtime

- Add ephemeral and durable local checkpointer composition.
- Implement start, resume, human decision, editable-agent interruption,
  cancellation, startup recovery, and projection repair.
- Port the implementation-review workflow, including reranking to zero to
  three critical comments, as the vertical slice.
- Keep old and new executors selectable per run during migration.

Done means killing the process at every node and interrupt boundary still
allows the SQLite run to resume exactly once without duplicating external
effects.

### 5. Implement the Temporal graph runtime

- Replace the placeholder adapter with the plugin-backed worker and client.
- Register the same catalog graphs and translate runtime start/resume/cancel
  operations to Temporal workflow calls, queries, and signals.
- Route all I/O nodes to Activities with tested timeout, retry, heartbeat, and
  idempotency policies.
- Project run state and domain audit events into Postgres.

Done means the local and Temporal conformance traces have the same logical node
outcomes, interrupts, outputs, and terminal status.

### 6. Migrate data and remove the interpreter

- Create `workflow_runs` rows for existing `run_states` and transform useful
  `run_events` into the new audit schema.
- Keep active legacy runs on the legacy executor; do not synthesize LangGraph
  checkpoints from an arbitrary reducer position. Drain them or require an
  explicit per-version migration.
- New runs use LangGraph behind a feature flag, followed by a release with
  dual-read/legacy-resume support.
- After no supported active run needs the old path, remove `run_states`,
  `run_events`, `WorkflowDefinition`, the DSL compiler, reducer/interpreter,
  definition serialization, and legacy executor.
- Update architecture documentation and remove the feature flag in a later
  release, not the same release that first writes LangGraph checkpoints.

Done means a database from the last supported release upgrades without data
loss, completed runs remain inspectable, and unsupported active legacy runs are
reported before any destructive migration.

## Removal inventory

The final cleanup is expected to remove or substantially replace:

- `packages/engine/src/openengine/__init__.py`: replace DSL constructors with
  registration, node, state, and context APIs;
- `packages/domain/src/engine/domain/workflow.py`: remove compiled step,
  template, transition, and definition types; retain reusable result/domain
  types in more appropriate modules;
- `packages/domain/src/engine/domain/state.py`: split reducer state into the
  product `WorkflowRun` and graph state schemas;
- `packages/engine/src/engine/core/workflow_interpreter.py` and workflow paths
  in `decide.py`: remove after legacy runs drain;
- `packages/runtime/src/engine/runtime/workflow_execution.py`: replace with
  runtime-neutral graph operations and local/Temporal adapters;
- `packages/runtime/src/engine/runtime/workflows.py`: replace definition
  loading with the versioned decorated-class catalog;
- workflow portions of the `StateStore` port and SQLite/memory serializers:
  replace reducer persistence with `WorkflowRunRepository`;
- legacy reducer/DSL acceptance tests: retain their user-visible traces as
  cross-runtime graph conformance fixtures.

Conversation, agent, approval, session grant, workspace, source-control, and
planning ports are reused by predefined nodes and are not part of the removal.

## Acceptance criteria

- A workflow author can define and register a `StateGraph` through the proposed
  class API without using OpenEngine transition constructors.
- One graph definition passes the ephemeral local, durable SQLite, and
  Temporal/Postgres conformance suites.
- SQLite runs resume after process termination before a node, after an external
  effect, after a checkpoint, and while awaiting human input.
- Temporal replay passes and every node has a validated execution location.
- Human review and editable-agent continuation preserve their current API and
  UI behavior in both runtimes.
- Run list/detail endpoints do not inspect LangGraph's private SQL tables and
  completed runs remain readable after their graph code is retired.
- Retried nodes do not duplicate comments, approvals, agent records, pushes,
  or published change requests.
- Existing SQLite application data is retained; old execution tables are
  dropped only after a backup-tested, non-destructive compatibility release.
- The final codebase has one graph semantics implementation (LangGraph), one
  execution truth per deployment, and no fallback custom interpreter for new
  runs.

## References

- [LangGraph persistence and checkpointers](https://docs.langchain.com/oss/python/langgraph/persistence)
- [`AsyncSqliteSaver` package and current schema](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite)
- [Temporal Python LangGraph integration](https://docs.temporal.io/develop/python/integrations/langgraph)
