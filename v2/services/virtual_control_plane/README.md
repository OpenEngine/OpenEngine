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
`workspaces.{workspace_id}.destroy`, `capabilities.get`, `workspaces.list`,
`workspaces.{workspace_id}.get` and
`operations.{operation_id}.get` through the shared messaging routes. The smolvm
adapter (below) is registered only when `OE_VCP_SMOLVM_IMAGE` is set, and the
Git worktree adapter only when `OE_VCP_WORKTREE=1` and daemon credentials
are; without either, capabilities remain empty and creation returns
`UNSUPPORTED_CAPABILITY`.
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
the CLI. Daemon installation, guest credentials and agent
execution are later tickets.

`tests/smolvm_fake.py` is a deterministic CLI stand-in (machines as JSON
records, a guest directory per machine, real `sh`/`git`/`tar` behind
`machine exec`, injectable faults) so routine CI needs no virtualization.
`tests/test_smolvm_provider.py` covers the adapter against it;
`tests/test_provider_conformance.py` runs the managed lifecycle cases --
create, idempotent ensure, explicit destroy, ownership mismatch, failed
preparation, restart reconciliation and capability reporting -- against both
the host-shaped double and the smolvm adapter. `tests/test_smolvm_smoke.py` is the opt-in real
smoke test (create, restart, ensure, delete):

```sh
OE_VCP_SMOLVM_IMAGE=alpine:3.20 uv run --project v2 --frozen pytest \
  v2/services/virtual_control_plane/tests/test_smolvm_smoke.py -s
```

### Git worktree adapter

`providers/worktree/provider.py` implements `AllocationProvider` as provider
`process` on macOS and Linux with the host's `git`. It needs no VM binary,
image or guest cgroup. It is enabled with `OE_VCP_WORKTREE=1` and owns
`worktrees/` inside VCP's exclusively locked state directory
(`OE_VCP_STATE_DIR`), so one VCP process owns every worktree it manages. Each
runtime owns `worktrees/{runtime_id}/`:

| Path | Holds |
| -- | -- |
| `owner.json` | provider, workspace, runtime, generation and, per repository, the source's real path, the commit its `ref` resolved to and the worktree path |
| `workspace/` | the workspace root an agent sees: `repositories/{index}` worktrees, context files, `AGENTS.md` |
| `state/` | the workspace daemon's private (`0700`) journal, a sibling of the root |
| `context.json` | digests of the context files and `AGENTS.md` VCP wrote into the root |
| `ready`, `daemon.json`, `daemon.log` | preparation marker; daemon PID hint and output |

The Workspace contract is used as it is; no field was added. A repository
`url` must be an absolute local path or `file://` URL (a worktree needs a
local repository) whose real path lies beneath one of the operator's
`OE_VCP_WORKTREE_SOURCE_ROOTS` (`os.pathsep`-separated); with none configured,
no repository can be named, so a caller cannot expose an arbitrary host
repository. `ref` is a branch, tag or commit, defaulting to the
source's `HEAD`, and is resolved once to a commit before anything is created.
`resources` are rejected with `INVALID_ARGUMENT` because a host process
cannot enforce them, and context paths must be relative without `.`/`..` and,
when repositories are requested, outside `repositories/`.

Ownership is persisted before side effects. VCP commits the runtime's
ownership at acceptance, and the adapter writes `owner.json` into a pending
directory that is renamed into place, so the runtime directory and its record
appear together. A directory under that name without a matching record, a
symlink in its place, or a record naming a worktree outside its own directory
is `OwnershipMismatch`: never adopted, prepared or removed. Each worktree is
created detached with `git worktree add --lock --reason "openengine-vcp
{workspace_id} {runtime_id}"`. Before adoption or cleanup the adapter checks
both records: `owner.json`, and Git's registration of that exact path whose
metadata points into the recorded source, under no other lock reason.

Preparation is idempotent until `ready` names the runtime; nothing can run in
the workspace before that. A resumed provision reuses the recorded commit,
keeps a worktree it already registered (forcing its checkout back to the
commit), re-adds a registration whose directory vanished (`add --force
--force`), and replaces an unregistered directory left inside its own
runtime. It never creates a second worktree. Context files are written
through directory descriptors with `O_NOFOLLOW`, so a symlink, including one
committed in the repository, cannot carry a write outside the root.
`ensure` never recreates anything: a missing directory or stale or missing Git
metadata is a terminal `PROVIDER_ERROR`.

The adapter never touches the source repository's working tree, other
worktrees or branches. It runs only `rev-parse`, `worktree list/add/lock/
unlock/remove` and, inside its own worktrees, `status`, `checkout` and
`for-each-ref`, with inherited `GIT_*` variables stripped and hooks, `fsmonitor`
and remote transports disabled (`-c core.hooksPath=/dev/null -c
core.fsmonitor=false -c protocol.allow=never`), so a source repository's hooks
cannot run commands as VCP. Filter drivers in a source repository's own config
still run on checkout, which is why sources are limited to operator-allowed
roots. VCP creates no
branch and deletes none, so branches an agent creates or checks out stay in
the repository after destroy.

**Cleanup policy.** Before anything is stopped or removed, destroy refuses
(terminal `PROVIDER_ERROR`, ownership kept, workspace `failed`) when an owned
worktree has uncommitted, untracked or ignored files, has a `HEAD` commit that no
branch or tag contains, still exists after Git lost its metadata, when
another worktree is registered inside the runtime directory, or when the
workspace root holds anything outside the worktrees that VCP did not write
(an agent's scratch output, or a context file changed since `context.json`
recorded it). A stale
registration whose directory is already gone is removed. The operator can
authorize discarding work with `OE_VCP_WORKTREE_DISCARD_CHANGES=1` (`worktree
remove --force`); it still deletes no branch. A workspace that never became
ready has nothing of an agent's and is removed without the policy. Removal
opens every directory below the runtime directory with `O_NOFOLLOW`, and a
worktree path reached through a symlink is refused, so swapping a parent for a
symlink cannot redirect deletion outside the owned root. Removal deletes
`owner.json` last, so an interrupted destroy resumes. Directory removal runs
off the event loop.

The record's generation must match: the daemon's journal is sealed to the
generation that created it, so another generation of the same runtime is an
`OwnershipMismatch`, never adopted, restarted or removed. Replacing a
generation needs fencing first (#1055); today the lifecycle only creates
generation 1.

**Host daemon.** `providers/worktree/daemon.py` runs `python -m
workspace_daemon` for the runtime (workspace root, separate journal
directory, binding, `--initialize` only before the workspace is ready) in its
own session, so it outlives VCP restarts. VCP waits for its ready file. The
daemon registers through #1118's `runtime.ready` update, which `__main__`
commits through `RuntimeUpdateListener` when this adapter is configured; no
execute route is served yet. Liveness and termination come from the daemon's
exclusive journal `flock`: a free lock is proof no daemon owns the journal.
`ensure` rediscovers a running daemon and restarts a dead one on its
established journal. Destroy, which the lifecycle already refuses while
attempts are active, sends SIGTERM and then SIGKILL to the daemon's process
group, after `ps` identifies the session leader whose command line runs this
journal's daemon. `daemon.json` only breaks ties, so a daemon started just
before VCP crashed, with no record yet, is still found and stopped. It
removes files only once the lock is free. An unidentifiable lock holder or
unconfirmed termination is a retryable `UNAVAILABLE` that keeps everything.
Daemon credentials come from `OE_VCP_RUNTIME_CREDENTIALS`;
`OE_VCP_RUNTIME_LOCAL_TEST_ACCOUNT=1` is the explicit test-only alternative.
That one file is passed to every daemon on the host: it is not yet scoped to
`oe.internal.v1.vcp.runtimes.{runtime_id}.*` per runtime, so one runtime's
daemon could act for another until per-runtime credentials are issued. Each
daemon gets only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `TMPDIR` and `TZ` from VCP's environment, never its secrets.
Without either, the adapter is not registered. `OE_VCP_WORKTREE_TOOLS`
(default `git,terminal`) sets the advertised tools; preparation checks they
exist on the host.

**Isolation is `process`, nothing more.** A worktree separates files, not
credentials, network or processes: the daemon and anything it later starts
run as the VCP user with the host's network. The journal is outside the
workspace root, and context paths cannot reach it, but a host process of the
same user could still write it. OS-level separation, agent launch and
process-tree fencing are #1055. Capabilities report `process` isolation, no
resource limits, checkpoint, suspend or restore.

Tests (macOS and Linux; skipped elsewhere): `tests/test_worktree_provider.py`
uses real temporary repositories and a lock-holding daemon stand-in.
`tests/test_provider_conformance.py` runs every managed lifecycle case against
the adapter beside the host double and smolvm. `tests/test_worktree_nats.py`
starts `python -m virtual_control_plane` and the real daemon over a real
`nats-server`, then creates, restarts VCP, rediscovers with `ensure` and
destroys over public subjects. No schema migration was needed: the selected
backend and runtime binding already persist in `workspace_runtimes`, and the
locator is opaque JSON.

### Explicit destruction

`workspaces.{workspace_id}.destroy` requires `retention.artifacts` and
`retention.checkpoints`, each `retain` or `delete`, with no defaults. Version
checks run before acceptance. Ready, failed and suspended workspaces can be
destroyed; active attempts are rejected with `INVALID_STATE` by an application
rule. Execution admission must maintain `active_attempt_ids` and refuse new
attempts once state is `destroying`; a cancelled attempt stays active until
its termination is confirmed (see "Cancellation"). A concurrent lifecycle operation returns retryable `UNAVAILABLE`.

Acceptance commits the immutable receipt, retention choices, pending operation
and `destroying` state/version before any deletion. `AllocationProvider.destroy`
receives persisted ownership and the optional opaque locator (failed provisioning
may never have saved one). It must check workspace, runtime **and generation**,
leave foreign compute untouched, and return only after confirming absence.
smolvm checks its named machine's labels, deletes with `machine delete --force`,
and lists again to confirm. An already absent machine succeeds without deletion;
an invalid or unavailable inventory cannot prove absence.

Only confirmed success atomically removes runtime ownership and the public
runtime, marks the operation succeeded and workspace destroyed, and appends one
`workspace.destroyed` event carrying the accepted retention choices. Acceptance
and completion each advance the workspace version. Replaying the command returns
the original receipt even after version changes; a fresh command on a destroyed
tombstone succeeds without another state change, provider call or event.

Pending/running destruction resumes with the same ownership after restart,
including a crash after deletion but before completion. Transient failures use
the same capped retry backoff and attempt limit as allocation. Terminal failure,
retry exhaustion or a provider missing on restart leaves ownership intact,
records a failed operation and visible workspace failure, publishes
`workspace.failed`, and frees the operation lock. Other workspaces continue.
Restore the provider or resolve the mismatch, then explicitly destroy with a new
command ID; the original command's receipt and failed outcome remain unchanged.

Artifact storage and checkpoints do not exist in this implementation. Both
retention choices are validated, persisted and reported, but have no stored
objects to act on. `retain` does **not** preserve files in compute: destruction
removes the machine and its workspace files. There is no backup, artifact export,
checkpoint retention implementation or automatic GC. Metadata, receipts,
operations and events remain for audit/replay. Service shutdown cancels workers
and leaves compute and ownership alone; it never initiates destruction.

### Where the ports go

`application/ports.py` defines the seams later tickets implement against:

- `WorkspaceProviderPort` (create / recover / destroy an opaque-locator
  workspace) -- the prototype port `application/workspaces.py`'s
  `WorkspaceService` drives; `tests/test_provider_conformance.py` runs the
  same lifecycle assertions against a worktree-shaped and a VM-shaped double.
- `AllocationProvider` -- the managed port above, implemented by
  `providers/smolvm` and `providers/worktree`; the same module's managed
  conformance cases run against the host-shaped double, smolvm and the worktree
  adapter.
- `RuntimeSupervisorPort` and `ExecutionContext` -- starting and fencing the
  agent process/session inside an already-provisioned workspace, kept apart
  from provisioning so a worktree never needs a VM to exist. #1055 (host
  fencing) implements it.
- `RuntimeTransport` -- sends one internal runtime request's exact bytes to
  the daemon its subject names by runtime ID only, and returns the reply bytes.
  `runtime_nats.py` implements it over Core NATS; see "Runtime delivery".
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

Automatic garbage collection, artifact storage,
checkpoint retention implementation, cancellation, guest daemon and agent
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
workspace documents and operations survive reopening and are retained indefinitely,
satisfying the public contract's minimum seven-day command-ID retention. Runtime
ownership survives until explicit destruction confirms compute absence. Disk usage therefore grows; future bounded retention
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

## Execution acceptance

`application/execution.py` accepts execution only when explicitly constructed
with `enabled=True` and an `ExecutionContextPort`. Production startup registers
session get/list queries but no execute command route, context adapter or runtime
registration handler. There is no environment flag to strand production work.
Tests seed owned runtime readiness and use a deterministic context adapter; they
do not demonstrate authenticated agent or model execution.

Acceptance requires a ready workspace with matching running runtime ownership,
a committed readiness record for that workspace/generation, a registered runner,
and available advertised concurrency. The context port resolves persisted
workspace state and validates operator policy for model, settings and task
inputs. Before writing, the application checks the exact internal wire bytes
against #1183's protocol, then verifies paths exist with the required types
inside the authorized filesystem root, including symlink containment. References
are rejected until an artifact authorization/storage port exists. Guest context
validation will require an adapter at the runtime boundary before production
execution can be enabled; no guest filesystem access is assumed here.

Alembic revision `700f6cbb335e` adds sessions, attempts, immutable generation
bindings, internal readiness records and a separate immutable binary command
outbox. One writer transaction includes all acceptance records, the public
receipt and the workspace's `active_attempt_ids`. Destroy uses the same writer
reservation, so destroy and execute cannot both win admission. New sessions
can run independently up to registered capacity; continuing an active, closed,
foreign-workspace or stale-generation session is rejected. Continuation also
requires identical agent configuration. Queries include starting attempts and
return at most the latest 20 attempts per session, with filtered cursor paging.
Session queries use read-only SQL pages with workspace/state indexes; retention
remains future work.

Replay precedes deadline, version, readiness and enablement checks. It returns
the exact accepted IDs even after restart or disabling execution; changed
immutable inputs under the same command ID conflict. Internal command bytes
are stored once and cannot be updated through the transaction port. The
allocation worker explicitly selects apply/ensure/destroy and never claims a
pending execute. `RuntimeCommands` (below) delivers the stored bytes.

`Execution.transition` is a trusted application projection primitive exercised
with deterministic fixtures, not a runtime update endpoint. It accepts only
starting→running/failed and running→completed/failed. A runtime command
acknowledgement has no task transition. Only running projects `task.started` and
succeeds execute; a later task failure leaves that operation successful, idles
the session and releases only the finished attempt. Failure before start fails
execute. State, operation and public event outbox changes commit together.
Before connecting this primitive to delivery, the owning slice must implement
authentication, generation fencing, journal ordering/deduplication and confirmed
termination evidence. Cancel, runtime transport/daemon, real executors and
approval/artifact storage are excluded.

Design/test evidence: #1045 at `891491c936e7078e8f10619566185d6150b942c7`;
only acceptance and transition semantics were adapted to current main's
transaction ports and merged #1284/#1183. Public schemas and #1043's single
`openengine.cc/v1alpha1` resource API group are unchanged. Generated schema and
architecture Mermaid sources accompany the revision; the schema documentation
workflow renders SVGs.

Context preparation and path checks run outside the writer transaction. Acceptance
rechecks ownership, readiness, session state and capacity, and rejects a changed
specification or runtime binding before persisting any acceptance records.

## Runtime delivery (#1118)

`application/runtime_delivery.py` connects accepted execution to a separately
supervised workspace daemon (`../workspace_daemon`) over the internal
workspace-runtime protocol. Nothing here is a public route: internal subjects
never reach the shared `Server`/`Authorizer`, and a guest connection never
relaxes `LocalOperator`. Public execute is still authorized by the shared
Server before acceptance writes anything, so a denied request creates no
session, attempt or command bytes, and nothing reaches a daemon. Internal
messages are instead checked against VCP's own ownership registry
(`workspace_runtimes` and the workspace snapshot): an unowned runtime is
`IDENTITY_MISMATCH` and a retired or unprovisioned generation `STALE_RUNTIME`,
before any deduplication state is read.

`RuntimeCommands` reads pending `runtime_commands` rows and sends each row's
exact accepted bytes through `RuntimeTransport` (`NatsRuntimeTransport`, the
shared client's envelope-free `Client.send` with one 5-second attempt). A lost
request or reply leaves the row pending; it is resent unchanged with capped,
jittered backoff (250 ms to 10 s, `openengine.messaging.retry.Backoff`), also
after a VCP restart. A reply is trusted only after `validate_wire_message` and
`validate_exchange` match its identity and binding. Revision `cfed9cf520fd`
adds `status` and the verified `acknowledgement` bytes beside the immutable
`wire`, and an `attempt_id` foreign key to `attempts`, backfilled from each
accepted command's target; `settle_runtime_command` can change only `status`
and the acknowledgement, once.

| Daemon reply | Row status | Public effect |
| -- | -- | -- |
| `accepted` (durable ownership) | `acknowledged` | none; the attempt stays `starting` |
| `UNAVAILABLE` (not ready or at capacity) | pending | retried unchanged |
| `INVALID_ARGUMENT`, `UNSUPPORTED_EXECUTOR`, `SESSION_BUSY` | `rejected` | attempt and execute fail; session and active attempt released |
| `STALE_RUNTIME`, `IDENTITY_MISMATCH`, `COMMAND_ID_CONFLICT`, `ATTEMPT_CONFLICT` | `blocked` | none: not evidence that no executor exists, so the attempt stays active for reconciliation |

`RuntimeUpdates.receive` handles `oe.internal.v1.vcp.runtimes.*.updates`
(`RuntimeUpdateListener`, queue group `vcp-runtime`). In one transaction it
fences the binding, deduplicates on a digest of the whole update keyed by
runtime and event ID (`runtime_updates`: runtime ID → `runtime_status`, event ID,
sequence unique per runtime, digest; insert-only), requires the next
contiguous runtime sequence and attempt sequence, applies the projection and
advances `runtime_status.sequence`; only then does it reply `committed`. A
duplicate commits with no effect; changed content under a known event ID, an
occupied sequence, a non-contiguous attempt sequence, an invalid transition or
an unsupported kind is `EVENT_CONFLICT` and quarantines the runtime (`ready`
false); a gap is `EVENT_GAP` with `expected_sequence`. Any storage failure rolls
the whole update back and sends no reply, so the daemon retries it. A
`runtime.ready` registers executors, concurrency and the stable `journal_id`
(a different journal is `IDENTITY_MISMATCH`), and is retryable `UNAVAILABLE`
while the owned workspace is still provisioning. Admission already requires
committed readiness for the current generation.

| Update | Projection (through `Execution.transition` or `Execution.progress`) |
| -- | -- |
| `attempt.started` | attempt running; execute succeeds; `task.started` |
| `attempt.progress` `conversation` | next session history sequence (kept beside the session snapshot); `conversation.message` |
| `attempt.progress` `usage` | delta added once to the attempt's `usage`; `usage.reported` with its period |
| `attempt.completed` | attempt completed, session idle, attempt released; `task.completed` |
| `attempt.failed` | attempt failed, session idle, attempt released; `task.failed`; execute fails only if never started |
| `attempt.cancelled` | attempt cancelled, session idle, attempt released; `task.cancelled`; execute fails `INVALID_STATE` only if never started |

Every terminal projection also settles each pending `workspaces.cancel`
operation of the attempt with its `final_state`.

Progress applies only to a started (running or cancelling) attempt and only for kinds the runtime
registered in `runtime.ready` (the codex executor registers `conversation` and
`usage`; `fake` none). A usage period that ends before it starts or overlaps
the attempt's previous one, an unregistered kind (including `artifact`),
progress after a terminal record, a cancelled update that does not name the
winning cancel's internal command and cause, and steering are
`EVENT_CONFLICT` and quarantine the runtime. Deduplication on the update's
event ID means redelivered progress never repeats history or usage. There is no
session-history query yet: the public events are the history. `inspect` asks the
daemon for read-only snapshots and its journal high-water mark.

Delivery, journal identity, sequencing and projection never read a locator,
VM ID, guest address or path (`tests/test_boundaries.py`); placement adapters
resolve the context root through `ExecutionContextPort` before acceptance.
Production startup still registers no execute route or delivery worker: that
needs a placement's context adapter and agent execution (#1122 smolvm, #1055
worktree). With the worktree adapter configured, it registers the update
listener so the daemons it supervises can commit `runtime.ready`. Tests
assemble the same components the service will.

`tests/test_runtime_delivery.py` runs in process (identical encoding, replay
and projection for worktree- and VM-shaped context doubles; lost command and
update acknowledgements; gaps, duplicates, conflicts, stale and wrong bindings
with a negative control that removes the fence; rollback; daemon rejections;
readiness during provisioning) and against `python -m workspace_daemon` as a
host subprocess over a real `nats-server` (completion, task failure, inspect
during execution, VCP restart before delivery and during execution, daemon
SIGKILL mid-task, execute replay, a denied public request and forged internal
messages). Delivery is at least once; there is no exactly-once transport. An
interrupted launch is never rerun.

## Cancellation (#1120)

`workspaces.{workspace_id}.cancel` is registered beside execute (the same
opt-in `Execution(enabled=True)`; production startup registers neither yet).
After the shared Server authorizes it, one transaction writes the receipt, a
pending `workspaces.cancel` operation, the attempt's `cancelling` state (in the
attempt and its session snapshot) and one immutable internal cancel command
(`runtime_commands`, its own subject), validated before the first write. The
command carries the attempt binding, the original execute identity and the
cancel's public cause. No schema change is needed: the winning cancel's
internal command ID, cause and coalesced operation IDs live in the attempt
document.

| Cancel of | Result |
| -- | -- |
| an unknown attempt, or one of another workspace | `NOT_FOUND` |
| a starting/running attempt whose runtime binding is no longer owned | `INVALID_STATE` |
| a starting or running attempt | pending operation, attempt `cancelling`, one internal cancel |
| a cancelling attempt | another pending operation, coalesced onto the winning cancel; no new internal command |
| a completed, failed or cancelled attempt | operation succeeds at once with that `final_state` |

The attempt stays in `active_attempt_ids` while cancelling, so its session,
the runtime's capacity and destroy's `INVALID_STATE` guard hold until the
daemon's terminal update confirms termination: cancel acceptance alone never
permits destroy. `attempt.started` for a cancelling attempt succeeds execute
and publishes `task.started` but keeps it cancelling. `attempt.cancelled`
publishes one `task.cancelled` whose `operation` is the execute and whose
`cancelled_by`, causal path and reason are the winning cancel's; a
`completed`/`failed` update that won the race keeps that outcome. Either way
every pending cancel operation succeeds with the attempt's `final_state` in
the same transaction. Cancel delivery uses the same dispatcher, retries and
reply checks as execute; a cancel rejection is never read as evidence that
nothing launched. Destroy-as-cancel, suspend/checkpoint cancellation, workflow
or GE job cancellation and approvals are out of scope.

`tests/test_cancellation.py` covers, in process, cancel before delivery
(tombstone), while recorded, launching and running, duplicate and replayed
cancels, lost cancel and update acknowledgements, completion winning the race,
unconfirmed termination keeping destroy refused, and rejections; and over real
NATS with the daemon subprocess, a daemon SIGKILL while cancelling and a
cancel accepted before delivery across VCP and daemon restarts, each ending in
exactly one `task.cancelled`, no surviving daemon child processes, and destroy
accepted only afterwards. The daemon there runs the in-process fake executor.


## Active fake steering and session history (#1121)

The same opt-in `Execution(enabled=True)` registers `workspaces.{workspace_id}.steer`.
It requires the session's single running attempt in the owned runtime generation
and an advertised fake steering capability. Codex, idle/starting/cancelling/closed
sessions and restore-dependent requests reject before acceptance. Receipt, operation
and immutable runtime command commit together. Retries keep the original binding.

Daemon steering outcomes project one operation, public event and history entry.
Terminal task projection fails all unapplied pending steering in that transaction,
including undelivered commands; it retires their delivery with `settled` without
inventing a daemon acknowledgement. Task outcomes keep their original result.
Late updates cannot reverse steering outcomes or append another history entry.

`sessions.{session_id}.events.list` pages conversation and steering entries in
ascending session sequence, with kind and attempt filters. Opaque versioned cursors
persist in the service database, bound to the authenticated tenant, session and
normalized filters. Retention is capped at the newest 10,000 cursor records across
the service database; evicted cursors return `INVALID_CURSOR`, and clients must
restart pagination. Issuing a cursor evicts excess records in the same transaction.
Each page uses bounded index seeks in one database snapshot.
The service-owned migration backfills conversation entries from retained outbox
envelopes, including already published messages. Usage/tool-call production is unchanged.

`tests/test_steering.py` covers receipt replay/conflict, rejection, rollback,
completion/cancel races, undelivered settlement, late/duplicate updates, database
reopen at acceptance/application/projection, no retargeting on continuation,
mixed history/filter pagination and cursor isolation. Its NATS test uses the
fake daemon subprocess and reuses history cursors after VCP restart. None of this
establishes authenticated model execution; real steering and restoration remain
outside this slice. Production startup keeps the existing execution opt-in boundary.
