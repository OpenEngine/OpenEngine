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

Only `capabilities.get` is served, and it always reports no providers: this
slice registers none. Workspace commands (`workspaces.apply`, `.execute`,
...) are out of scope here.

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
  transactional document and command-receipt port implemented by
  `persistence/durable.py` for SQLite. `ports.py` re-exports `UnitOfWork`.
  `EventPublisher` remains a delivery port for #1112.

Provider selection is sticky and persisted: `workspaces.locator` (added by
the `add workspace locator` migration) holds whatever opaque
ownership/placement metadata the provider that created a workspace needs to
reassert ownership of it, including after this process restarts. Recovering
with an unregistered or unavailable provider raises rather than silently
reprovisioning with a different one or a lower-isolation fallback --
`tests/test_workspaces_service.py` covers both failures.

### Out of scope here

Real worktree/smolvm adapters, VM allocation, agent execution, workspace
commands, and authenticated public (non-loopback) callers (#1051, which plugs
into the same `Authorizer` boundary once it exists).

## Durable lifecycle persistence

Run `PERSISTENCE.upgrade(config)` before opening
`DurableStore(PERSISTENCE.database(config))`. Alembic owns all tables; opening
or using the store never creates schema. The new revision extends
`d8f92154b5f7` and leaves the prototype `workspaces` table and its locators
intact. Prototype rows are **not** adopted as managed compute.

The transaction port stores JSON snapshots in separate collections:

| Collection | Contents owned by the lifecycle application |
| --- | --- |
| `workspace_records` | Workspace document, resource version, lifecycle state |
| `operations` | Operation identity and progress/terminal state |
| `runtime_ownership` | Provider, opaque locator, logical runtime ID and generation |
| `cursors` | Durable consumer sequence/cursor state |
| `outbox` | Lifecycle events awaiting delivery |

Receipt bodies are private to `replay` and `record_command`; the document
API cannot overwrite them. A validated command's identity consists of its
subject, payload, expected version and causal path, indexed by command ID.
JSON object key order does not matter. `deadline_at` is excluded: accepted
commands still replay their original response and operation ID after expiry
or when a retry changes/removes its deadline. Changed immutable inputs raise
`CommandIdConflict` (`COMMAND_ID_CONFLICT`). Non-retryable rejections are
retained; retryable rejections are not.

Use one short `async with store.transaction() as tx` block to check
`tx.replay(subject, command)` **before** deadline/version checks or mutations.
If there is no receipt, write the workspace, operation, ownership, cursor and
outbox records as needed with `tx.put`, then call
`tx.record_command(subject, command, response)`. A clean exit commits all
records; an exception or cancellation rolls them all back. Repeated receipt
insertion always returns the original response. No operation failure
implicitly clears ownership or changes workspace version: reconciliation
must explicitly update those independent records. Never perform provider
or network calls while holding the transaction.

SQLite `BEGIN IMMEDIATE` reserves the writer before receipt lookup, including
across independent connections/store instances. SQLite supports only one
writer at a time, with the shared layer's five-second busy timeout; contention
beyond that fails rather than pretending acceptance. The service still
requires #1187's exclusive state-directory lock. This is a single-host
foundation, not PostgreSQL/HA support.

There is no automatic pruning. Receipts (including original responses),
ownership, operations, cursors and outbox records survive reopening and are
retained indefinitely, satisfying the public contract's minimum seven-day
command-ID retention. Disk usage therefore grows; future bounded retention
must preserve the retry window and unreconciled ownership. Event delivery,
lifecycle command handlers, provider effects, sessions and attempts remain
in their follow-up tickets. These tests prove persistence with deterministic
JSON records; they do not execute an agent or allocate compute.
