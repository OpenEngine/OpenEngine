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

The service serves `workspaces.apply`, `workspaces.{workspace_id}.ensure`,
`capabilities.get`, `workspaces.list`, `workspaces.{workspace_id}.get` and
`operations.{operation_id}.get` through the shared messaging routes. The smolvm
adapter (below) is registered only when `OE_VCP_SMOLVM_IMAGE` is set; without
it capabilities remain empty and creation returns `UNSUPPORTED_CAPABILITY`.
Readiness promises request handling, not a VM or executor. Later commands
return the shared explicit unsupported response.

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

`AllocationProvider` in `application/ports.py` is the managed provider port,
separate from the prototype's unkeyed `WorkspaceProviderPort.create`. Every
call receives the persisted `Ownership` (workspace ID, runtime ID, generation),
written at acceptance before any provider call. `validate` applies
provider-specific admission rules inside acceptance (pure, no I/O);
`provision` creates or finishes creating the owned compute and prepares it;
`ensure` reasserts ownership of existing compute and makes it running and
prepared without ever reprovisioning compute that is gone or adopting foreign
compute; `context` describes where work happens (root, working directory,
instruction files) so command acceptance and delivery never hard-code guest
paths. `provision` and `ensure` must be idempotent across process restarts,
converging on the same compute even when an earlier call was interrupted after
allocation. A provider that cannot honor this must not be registered.
Capabilities come from the configured adapter, never from an assumed
isolation level. Provider identity and the opaque locator are persisted per
workspace; reconciliation always uses the original provider.

One worker, under the service's exclusive state-directory lock, resumes
unfinished operations on startup and polls every second. It claims an
operation as `running` in its own transaction, then runs the provider outside
any transaction (bounded by `ALLOCATION_TIMEOUT_SECONDS`, 10 minutes, since a
first boot may pull an image and clone repositories). A restart finds both
`pending` and `running` operations through the `ix_operations_active` partial
index and repeats them with the same ownership. Retryable provider errors
(`UNAVAILABLE`) and timeouts leave the operation `running` and schedule a
retry after 1, 2, 4, ... seconds, capped at 60 (`retry_delay`), recorded on
the operation record; the worker skips operations that are not yet due. After
`MAX_ATTEMPTS` (8) failed attempts the operation fails with `PROVIDER_ERROR`
carrying the last error. Terminal errors atomically mark the operation failed
and, for creation, the workspace failed with a `workspace.failed` event.
Ownership is retained for cleanup in every failure. Success stores the opaque
locator, marks the runtime ready and operation succeeded, and appends
`workspace.ready`. Terminal transactions re-read the workspace document and
change only the fields the operation owns, so state another producer wrote
while the provider ran survives. Shutdown cancels the provider call before
closing persistence; it never destroys compute.

`workspaces.{workspace_id}.ensure` accepts for a `ready` workspace whose
provider is configured: it records a pending `workspaces.ensure` operation and
takes the workspace's operation lock (`active_operation_id`, private to the
document). A second lifecycle command while one is in progress is rejected
`UNAVAILABLE` (retryable, so the same command ID is accepted once the lock is
free); a workspace in any other state is `INVALID_STATE`; a missing one is
`NOT_FOUND`; restoring a suspended workspace is `UNSUPPORTED_CAPABILITY`. The
worker runs the provider's `ensure`. Success records
`{runtime_id, runtime_generation, restored: false}` and releases the lock
without changing the workspace's state, version or timestamps and without a
second `workspace.ready` event. A failed ensure fails only the operation: the
workspace stays `ready` and keeps its ownership.

If a workspace's provider is not configured when its accepted operation runs
(for example after a restart with a changed configuration), the operation
fails explicitly with `PROVIDER_ERROR` ("provider X is not configured on this
host") and releases the operation lock, so unrelated queued work proceeds.
Creation marks the workspace failed; an ensure leaves it as it was. Runtime
ownership is retained: once the provider is configured again a new command
can ensure or clean up the compute. VCP never reinterprets the workspace for
another provider or falls back to host execution.

Workspace list uses descending `(created_at, workspace_id)` keyset pagination.
Versioned base64url JSON cursors contain that key and normalized filters;
no process memory, database cursor table or rotating signing key is required.
Cursors survive restart and reject malformed tokens or changed filters.
Limits may change between pages. These are live views, not frozen snapshots.
Name lookup and filtered keyset pages use database expression indexes; listing
uses a read-only connection without reserving the writer lock. Pending allocations
use a partial index and up to eight independently refilled slots. Provider calls
time out after 60 seconds, retaining pending state for idempotent retries.
Alembic revisions add these indexes to existing stores.
Receipts, operations and ownership use the existing durable tables.

`tests/test_lifecycle.py` exercises real NATS with the shared Client/Server,
including authorization, a public broker reached via loopback, lost-reply
retries and restart, concurrent retries, typed queries and schema errors.
It also covers interrupted allocation, terminal failure, retry backoff and its
cap, idempotent ensure, pending and running create/ensure recovery across
restarts, explicit failure of operations whose provider disappeared, cursor
restart/filter binding, durable rejections and acceptance rollback.

### smolvm adapter

`providers/smolvm/provider.py` implements `AllocationProvider` with the
released `smolvm machine` CLI (`create`, `start`, `status`, `ls`, `exec`,
`cp`); the application layer never imports it (`tests/test_boundaries.py`).
A workspace's machine is named `oe-vcp-{runtime_id}` and labelled
`oe.workspace`, `oe.runtime` and `oe.generation`. Provisioning lists machines
first: its own half-made machine is reused after a crash, and a machine under
that name with different labels raises `OwnershipMismatch` -- it is never
started, prepared or deleted. The machine runs the configured image with
`sleep infinity` as its workload, outbound network, whole vCPUs (`cpuMillis`
must be a multiple of 1000, else `INVALID_ARGUMENT` at acceptance), `--mem`
and `--storage` from the spec. Preparation runs in the guest as idempotent
steps beneath `/workspace`: required tools are checked with `command -v`
(`terminal` means `sh`; `tar` always; `git` when repositories are requested),
repositories are cloned to `repositories/{index}` and checked out at `ref` if
given, context files and `instructions` (as `AGENTS.md`) arrive as one tar
archive through `machine cp`, and `/oe-vcp/ready` finally names the runtime.
An interrupted preparation resumes from the first unfinished step; a repeated
call reads the marker and stops. `ensure` refuses a vanished machine
(`PROVIDER_ERROR`, terminal) and restarts a stopped one. Context file paths
must be relative and stay beneath the root. No host directory or credential
is mounted or forwarded, and CLI diagnostics (which can carry repository
credentials) stay out of public errors.

Capabilities are honest: `microvm` isolation, the tools the host was
configured with, and no checkpoint, suspend or restore. CLI exits and
timeouts are retryable `UNAVAILABLE` errors for the lifecycle's capped
backoff; a missing tool, a vanished machine, an ownership mismatch and a
missing CLI binary are terminal. Registration is opt-in:
`OE_VCP_SMOLVM_IMAGE` names the guest image, `OE_VCP_SMOLVM_TOOLS`
(default `git,terminal`) the tools to advertise and `OE_VCP_SMOLVM_EXECUTABLE`
the CLI. Destruction, daemon installation, guest credentials and agent
execution are later tickets.

`tests/smolvm_fake.py` is a deterministic CLI stand-in (machines as JSON
records, a guest directory per machine, real `sh`/`git`/`tar` behind
`machine exec`, injectable faults) so routine CI needs no virtualization.
`tests/test_smolvm_provider.py` covers the adapter against it;
`tests/test_provider_conformance.py` runs the managed lifecycle cases --
create, idempotent ensure, ownership mismatch, failed preparation, restart
reconciliation and capability reporting -- against both the host-shaped double
and the smolvm adapter. `tests/test_smolvm_smoke.py` is the opt-in real
smoke test (create, restart, ensure, delete):

```sh
OE_VCP_SMOLVM_IMAGE=alpine:3.20 uv run --project v2 --frozen pytest \
  v2/services/virtual_control_plane/tests/test_smolvm_smoke.py -s
```

### Where the ports go

`application/ports.py` defines the seams later tickets implement against:

- `WorkspaceProviderPort` (create / recover / destroy an opaque-locator
  workspace) -- the prototype port `application/workspaces.py`'s
  `WorkspaceService` drives; `tests/test_provider_conformance.py` runs the
  same lifecycle assertions against a worktree-shaped and a VM-shaped double.
- `AllocationProvider` -- the managed port above, implemented by
  `providers/smolvm` and, in #1054, the Git worktree adapter; the same module's
  managed conformance cases run against both the host-shaped double and smolvm.
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

The worktree adapter (#1054), workspace destruction, guest daemon and agent
execution, checkpoint/suspend/restore, and authenticated public (non-loopback)
callers (#1051, which plugs into the same `Authorizer` boundary once it exists).

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
| `workspaces` | Workspace document, resource version, lifecycle state, private operation lock |
| `operations` | Operation identity and progress/terminal state, private retry schedule |
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
