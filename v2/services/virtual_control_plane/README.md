# Virtual Control Plane - VCP
The lifecycle manager of Agents, and the Workspaces they're using to get their work done.

Data Model
Workspace | A place in which an agent is doing work.
WorkspaceProvider | A virtual machine substrate providing the Workspaces | process vs smolvm & kubernetes
Approval | A request for approval awaiting decision.

## Service skeleton (this slice)

VCP starts on every `oe up` and direct `oe-supervisor` invocation,
after every other service and NATS;
it is stopped first, and stopping it never destroys a workspace. It connects
to NATS, serves its subjects through the shared `openengine.messaging`
`Server`, and answers `GET /healthz` only once it actually holds its state
directory's exclusive lock, is connected and is subscribed -- the same
truthful-readiness contract `ge` uses. Authorization runs through the shared
`Authorizer` boundary before any handler, defaulting to `LocalOperator`
(loopback only, judged by where the broker listens -- not the URL VCP
dialed).

The service serves `workspaces.apply`, `capabilities.get`, `workspaces.list`,
`workspaces.{workspace_id}.get` and `operations.{operation_id}.get` through the
shared messaging routes. Production registers no managed provider yet, so
capabilities remain empty and creation returns `UNSUPPORTED_CAPABILITY` until
an adapter is registered. Readiness promises request handling, not a VM or
executor. Later commands return the shared explicit unsupported response.

### Managed workspace creation

`application/lifecycle.py` accepts a Workspace resource under a new name with
`workspaces.apply`. Acceptance atomically stores the specification, public
workspace and pending operation, generation-one runtime ownership, immutable
receipt and `workspace.provisioning` event. No provider call runs in that
transaction. The shared Server authorizes before any business read, receipt
replay or acceptance. This remains local-operator mode, with no tenant or
project identity in the VCP contract; owner labels are references, not grants.

The receipt-aware handler retains schema/deadline/version/capability rejections
for well-formed command IDs. Invalid IDs cannot reserve receipts. Receipt
identity includes subject, payload, expected version and causal path; deadlines
are excluded. Retries replay before deadline or version checks. An unchanged
apply under another command ID returns the original operation without another
allocation; immutable specification changes are invalid, and other updates
remain explicitly unsupported in this creation slice.

`AllocationProvider` in `application/ports.py` is the managed allocation port,
separate from the prototype's unkeyed `WorkspaceProviderPort.create`.
`provision(runtime_id, workspace)` must be idempotent across process restarts,
returning/recovering the same allocation for the persisted runtime ID, even
when an earlier call was interrupted after allocation. A provider that cannot
honor this must not be registered. Capabilities come from the configured
adapter, never from an assumed isolation level. No real provider or executor
is added here; tests use deterministic fake compute.

One worker, under the service's exclusive state-directory lock, resumes pending
operations on startup and polls every 100 ms. Allocation runs outside the
transaction. Retryable provider errors leave the operation pending; terminal
errors atomically mark the workspace and operation failed and append a failure
event. Ownership is retained for cleanup. Success stores the opaque locator,
marks the runtime ready and operation succeeded, and appends `workspace.ready`.
An interrupted call repeats the same runtime ID. Shutdown cancels allocation
before closing persistence; it never destroys compute.

Workspace list uses descending `(created_at, workspace_id)` keyset pagination.
Versioned base64url JSON cursors contain that key and normalized filters;
no process memory, database cursor table or rotating signing key is required.
Cursors survive restart and reject malformed tokens or changed filters.
Limits may change between pages. These are live views, not frozen snapshots.
The current document store scans managed documents for name lookup, pending
operations and listing; this single-host implementation adds no new schema.
Receipts, operations and ownership use the existing durable tables.

`tests/test_lifecycle.py` exercises real NATS with the shared Client/Server,
including authorization, a public broker reached via loopback, lost-reply
retries and restart, concurrent retries, typed queries and schema errors.
It also covers interrupted allocation, terminal failure, retryable provider
errors, cursor restart/filter binding, durable rejections and acceptance rollback.

### Where the ports go

`application/ports.py` defines the seams later tickets implement against:

- `WorkspaceProviderPort` (create / recover / destroy an opaque-locator
  workspace) -- implemented by #1054's Git worktree/host-process adapter and
  #1114's smolvm adapter. `application/workspaces.py`'s `WorkspaceService`
  drives both through this one port, and
  `tests/test_provider_conformance.py` runs the same lifecycle assertions
  against a worktree-shaped and a VM-shaped double, so neither real adapter
  needs its own copy of this test.
- `RuntimeSupervisorPort` and `ExecutionContext` -- starting and fencing the
  agent process/session inside an already-provisioned workspace, kept apart
  from provisioning so a worktree never needs a VM to exist. #1055 (host
  fencing) and #1118 (durable fake workspace daemon delivery) implement it.
- `UnitOfWork` / `Transaction` (`application/transactions.py`) -- the
  transactional document, command-receipt and event-outbox port implemented
  by `persistence/durable.py` for SQLite. `ports.py` re-exports `UnitOfWork`.
- `EventPublisher` -- publishes one event and returns only after the broker
  acknowledges it; `jetstream.py` implements it for the `vcp-events` stream.
  See "Event outbox" below.

Prototype provider selection is sticky and persisted: `workspaces.body.legacy.locator`
preserves the opaque ownership/placement metadata the original provider needs to
reassert ownership of it, including after this process restarts. Recovering
with an unregistered or unavailable provider raises rather than silently
reprovisioning with a different one or a lower-isolation fallback --
`tests/test_workspaces_service.py` covers both failures.

### Out of scope here

Real worktree/smolvm adapters, VM allocation, agent execution, later workspace
commands, and authenticated public (non-loopback) callers (#1051, which plugs
into the same `Authorizer` boundary once it exists).

## Durable lifecycle persistence

Run `PERSISTENCE.upgrade(config)` before opening
`DurableStore(PERSISTENCE.database(config))`. Alembic owns all tables; opening
or using the store never creates schema. The new revision extends
`d8f92154b5f7` and replaces the prototype table with the document-shaped
`workspaces` table. Populated rows, timestamps and opaque locators are preserved
under `body.legacy`, keyed by their original IDs. The prototype adapter reads
and updates only those bodies; it ignores managed documents. Migration creates
no runtime ownership or lifecycle state for these rows: prototype rows are
**not** adopted as managed compute. Downgrade restores prototype rows (including
subsequent prototype updates), discarding managed lifecycle records.

The transaction port stores JSON snapshots in separate collections:

| Collection | Contents owned by the lifecycle application |
| --- | --- |
| `workspaces` | Workspace document, resource version, lifecycle state |
| `operations` | Operation identity and progress/terminal state |
| `workspace_runtimes` | Provider, opaque locator, logical runtime ID and generation |
| `command_receipts` | Immutable identity and original response (private receipt API) |

Receipt bodies are private to `replay` and `record_command`; the document
API cannot overwrite them. A validated command's identity consists of its
subject, payload, expected version and causal path, indexed by command ID.
JSON object key order and equivalent numeric spellings (`1` / `1.0`) do not
matter. `deadline_at` is excluded: accepted commands still replay their original response and operation ID after expiry
or when a retry changes/removes its deadline. Changed immutable inputs raise
`CommandIdConflict` (`COMMAND_ID_CONFLICT`). Non-retryable rejections are
retained; retryable rejections are not.

Use one short `async with store.transaction() as tx` block to check
`tx.replay(subject, command)` **before** deadline/version checks or mutations.
If there is no receipt, write the workspace, operation and runtime ownership
records as needed with `tx.put`, then call
`tx.record_command(subject, command, response)`. A clean exit commits all
records; an exception or cancellation rolls them all back. Repeated receipt
insertion always returns the original response. No operation failure
implicitly clears ownership or changes workspace version: reconciliation
must explicitly update those independent records. Never perform provider
or network calls while holding the transaction.

SQLite `BEGIN IMMEDIATE` reserves the writer before receipt lookup, including
across independent connections/store instances. SQLite supports only one
writer at a time, with the shared layer's five-second busy timeout; contention
beyond that fails rather than pretending acceptance. Cold connection setup
also retries SQLite BUSY during PRAGMAs within that timeout: concurrent first
connections can race to enable WAL before any transaction starts. These waits
yield to the event loop so another connection can release its lock. The service
still requires #1187's exclusive state-directory lock. This is a single-host
foundation, not PostgreSQL/HA support.

There is no automatic pruning. Receipts (including original responses),
workspace documents, runtime ownership and operations survive reopening and are
retained indefinitely, satisfying the public contract's minimum seven-day
command-ID retention. Disk usage therefore grows; future bounded retention
must preserve the retry window and unreconciled ownership. Creation handlers
and fake provider effects are covered by the lifecycle tests above; sessions and attempts remain in follow-up tickets. The persistence tests
use deterministic JSON records and do not execute an agent or allocate compute.

### Follow-up issue wording

#1111's scope and acceptance criteria still mention durable cursors, event-outbox
records and atomic event rollback. Update them to the four tables above and
receipt/operation/runtime rollback and restart coverage. Pagination can use
stateless tokens; JetStream durable consumers own their reading positions.
#1112 should explicitly own creation of its outbox schema together with its
publisher, including insertion ordering, workspace identity and indexed pending
publication state; it must not assume #1111 supplies a generic event table.
(#1112 now does; see below.)

## Event outbox

Revision `09a0811b905f` adds the `outbox` table on top of the lifecycle
schema, leaving existing records untouched. Each row has an `AUTOINCREMENT`
`sequence` (publication order), `workspace_id` (the partition), a unique
`event_id`, the immutable `envelope`, `created_at` and a nullable
`published_at`; `ix_outbox_pending` indexes unpublished rows by sequence.
Event IDs never determine order. Revision `c36db826eebb` adds the partial
`ix_outbox_workspace_pending` index on `(workspace_id, sequence)`. When some
workspaces are in backoff, the publisher seeks their neighbours in that index
and merges eligible workspace heads in sequence order, stopping at the batch
limit. Query work grows with the number of pending workspaces and the batch
size, rather than the number of events in blocked backlogs. Without exclusions,
the publisher uses the original sequence index directly.

Producers call `tx.append_event(envelope)` inside the same
`store.transaction()` as the workspace, operation, runtime and receipt
changes that caused the event, so a failure or cancellation rolls them all
back together. The envelope's `causal_path` must end with its `event_id`, and
a reused event ID fails the transaction. A command that `tx.replay` answers
must return the stored response without appending again.

`OutboxPublisher` (`application/outbox.py`) runs for the service's lifetime,
outside any lifecycle transaction. It reads pending rows in sequence order,
publishes each envelope unchanged to
`oe.v1.vcp.events.workspaces.{workspace_id}.{event_type}` on the existing
`vcp-events` stream with `Nats-Msg-Id` set to the event ID, and sets
`published_at` only after JetStream acknowledges it. If an event is not
acknowledged, later events of that workspace wait; other workspaces continue.
Publication failures (broker outage or missing stream) back off per workspace
exponentially from 0.1 s to 10 s and never discard a record. A successful
publication resets that workspace's backoff. Healthy workspaces keep draining;
`notify()` and idle polling can publish their new events without retrying a
blocked workspace early. The next retry deadline also wakes the worker, even
if the idle poll is longer. Database errors retain a shared bounded backoff.
It polls every second when idle; `notify()` wakes it sooner. On shutdown it stops between
events, cancels an in-flight publish after 5 s and closes before the NATS
connection; anything unacknowledged is published after the next start.

**Delivery is at least once.** A lost acknowledgement, or a crash after
acknowledgement but before `published_at` is written, republishes the same
event ID and envelope. JetStream drops repeats only within the stream's
2-minute duplicate window, so consumers must deduplicate on `event_id`.

The worker knows nothing about event types, so later task and conversation
producers append through the same port. The lifecycle handler appends creation and terminal allocation events through
this same outbox. This keeps the single-host,
single-writer constraint: there is no distributed publisher coordination, and
published rows are retained indefinitely (no retention/GC).
