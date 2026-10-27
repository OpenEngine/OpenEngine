# Workspace runtime protocol v1

This is the **internal contract**, not an implementation. It covers the next
execution milestone: execute, agent progress/outcome, cancellation and recovery.
Steering is reserved for a later slice. Approvals,
checkpoints and suspension are excluded. The public
[VCP contract](../../../virtual_control_plane/v1/asyncapi.yaml) is unchanged.
`asyncapi.yaml` is written from VCP's perspective; `messages.schema.json` is the
wire authority and this document specifies stateful rules. Release:
`1.0.0-draft.1`. Breaking changes require a new subject namespace.

## Subjects and envelopes

| Subject | Sender → receiver | Body → reply |
| -- | -- | -- |
| `oe.internal.v1.runtime.runtimes.{runtime_id}.execute` | VCP → daemon | execute → command_ack |
| `oe.internal.v1.runtime.runtimes.{runtime_id}.cancel` | VCP → daemon | cancel → command_ack |
| `oe.internal.v1.runtime.runtimes.{runtime_id}.inspect` | VCP → daemon | inspect → inspect_response |
| `oe.internal.v1.vcp.runtimes.{runtime_id}.updates` | daemon → VCP | update → update_ack |

All use native NATS request/reply. Replies go only to the transport reply inbox;
no envelope contains a reply address. The UTF-8 JSON body must fit within
1 MiB (1,048,576 bytes, inclusive). Enforce `len(message.data)` before UTF-8
decoding or JSON parsing, including wire whitespace and escape sequences. Do not
measure a reserialized object: its encoding can differ from the received bytes.
Senders also check the exact serialized bytes they will publish. The tooling's
`validate_wire_message` is the transport entry point; `validate_message` checks
only already-decoded structure and cross-field invariants. These subjects must not enter the public
`vcp-events` stream. This revision adds no broker stream or consumer. Durable
sender outboxes and receiver journals provide at-least-once delivery across
Core NATS disconnection. Internal acknowledgements are not public operation
responses and do not allocate new public operation IDs.

Every message binds a workspace ID, runtime ID and positive runtime generation.
Attempt messages additionally bind session and attempt IDs. The subject's
runtime ID must equal the body's runtime ID. VCP allocates all these identities
before delivery. One runtime ID belongs to exactly one workspace and generation;
a replacement always uses a new runtime ID and incremented generation.
`boot_id` changes on daemon process restart; it is diagnostic, **not** the
execution identity or a reason to discard journaled events. `journal_id` is
stable for the generation. Each attempt update also carries `attempt_sequence`, contiguous from 1 for that
attempt. Interleaved attempts have independent counters. Duplicate IDs keep both
counters unchanged. A new attempt starts at 1; a daemon restart resets neither.
An attempt counter mismatch is `EVENT_CONFLICT`; runtime gap replay must first
restore missing updates before checking the attempt counter.
Runtime event sequence numbers are global within
that generation, strictly increasing from 1 and never reset by a restart.

Internal command/event IDs are independent of public command/event IDs.
`execution.command_id` identifies the original internal execute command.
`execution.cause` carries the public execute operation reference and its
original causal path. Cancel also carries its own public `cause` and a fresh
internal command ID. A cause's path must end with its public command ID.
Internal hops do not append to that public path; VCP appends exactly one new
public event ID when projecting an update. This preserves the public 32-element
limit. Public acceptance must reserve that one element before creating an
attempt. Retries preserve the entire cause and all identities.

## Authentication and fencing

Envelope identities and arbitrary headers are claims, not authentication.
Provision each daemon with a separate NATS credential scoped to its runtime:
subscribe to its execute/cancel/inspect subjects **and** its own reply-inbox
prefix `_INBOX.oe.runtime.{runtime_id}.>`; publish to its own updates subject,
with restricted native response permissions for replies to incoming commands.
Response permissions permit publishing replies, not subscribing to inboxes.
Never grant a daemon `_INBOX.>` or another runtime's inbox prefix. VCP alone may
publish runtime commands and consume updates. Other callers have no internal
subject access. VCP checks updates against its provisioned ownership registry;
the daemon checks commands against its immutable provisioned binding. This
uses broker-enforced subject permissions, not an assumed publisher identity in
a NATS message. A shared unrestricted account is only a test fixture, never a
production authentication mechanism. Credentials are provisioned out of band;
no secret, bearer token, executable shell command or host path is part of this
protocol. Use TLS when the broker is outside a trusted local transport.

For example, the credential for `rt_demo` has this permission block (inside
its NATS user configuration):

```text
permissions: {
  publish: { allow: ["oe.internal.v1.vcp.runtimes.rt_demo.updates"] }
  subscribe: { allow: [
    "oe.internal.v1.runtime.runtimes.rt_demo.execute",
    "oe.internal.v1.runtime.runtimes.rt_demo.cancel",
    "oe.internal.v1.runtime.runtimes.rt_demo.inspect",
    "_INBOX.oe.runtime.rt_demo.>"
  ] }
  allow_responses: { max: 1, expires: "10s" }
}
```

Configure the daemon's nats-py connection **before any request**:

```python
connection = await nats.connect(
    servers=[broker_url],
    user_credentials=credential_path,
    inbox_prefix=f"_INBOX.oe.runtime.{runtime_id}",
)
```

The client appends its unique connection/request tokens to this prefix. Do not
include `>` or a trailing dot in `inbox_prefix`, and do not use the default
`_INBOX` prefix with these credentials. Reconnecting/restarting retains this
runtime-scoped configuration. Both multiplexed request/reply subscriptions and
explicit inboxes must stay under that prefix. VCP likewise needs its own
provisioned inbox prefix (for example `_INBOX.oe.vcp.{instance_id}.>`) and matching
client configuration for commands/inspect, plus response permissions to publish
update acknowledgements. No daemon may subscribe to the VCP inbox prefix.
The response permission expiry exceeds the protocol's 5-second request timeout;
a late/lost acknowledgement is recovered by an unchanged retry.

See [NATS authorization](https://docs.nats.io/learn/security/authorization) for
independent publish/subscribe permissions and response permissions.

Authenticate/authorize and validate the binding before consulting dedup state.
Reject wrong workspace/runtime routing as `IDENTITY_MISMATCH`; reject a retired
or incorrect generation as `STALE_RUNTIME`. Neither may mutate a session,
attempt or operation. Reject an unexpected journal replacement as
`IDENTITY_MISMATCH` and require operator/provider reconciliation. Revocation of
old runtime credentials and confirmation that old compute/executors stopped
must precede replacement; an incremented number alone cannot kill old work.
Only one daemon process may own a runtime journal and its executors (exclusive
process lock). The daemon holds this ownership until executor cleanup finishes.

## Acceptance and command delivery

1. VCP authorizes the public execute request, validates agent/config/context and
   the workspace's execution readiness, and selects a new or continued session.
   This milestone permits one active attempt per session. It atomically writes
   public command receipt, operation, session/attempt (`starting`), runtime
   binding and internal execute outbox entry, then replies publicly accepted.
2. The dispatcher sends the immutable internal execute command. The daemon
   validates runner support, paths, binding and session ownership; atomically
   journals command receipt and attempt (`recorded`); then replies accepted.
   This acknowledgement means **durable ownership**, not that an agent started.
3. A separate executor worker records launch intent (`launching`) before any
   process/ACP side effect. It launches at most once for that attempt identity.
   After confirming the executor/session started, it records `running` and a
   durable `attempt.started` update in the same transaction.
4. VCP commits that update, attempt/session state, execute operation success and
   the public `task.started` outbox event atomically, then replies committed.
   Task completion is separate from the already-succeeded execute operation.

Command IDs are scoped to the runtime generation. Compare canonical parsed
JSON (object key order ignored; array order preserved), exact subject, all
binding fields and complete body. An identical retry returns the original
receipt. Different contents under the same command ID fail with
`COMMAND_ID_CONFLICT`. A different execute command ID targeting an existing
attempt fails with `ATTEMPT_CONFLICT`, even when the task matches. Reusing a
session for concurrent work fails with `SESSION_BUSY`. Remember terminal
rejections; `UNAVAILABLE` is not remembered. Do not retry a permanent rejection
with a fresh internal ID to bypass it: VCP records a failed public operation and
attempt, releasing the session after establishing that no executor started.

Use a 5-second transport timeout and exponential backoff with jitter (250 ms
initial, 10 seconds maximum). Timeout means unknown outcome: retry unchanged.
Public deadlines constrain public acceptance only and are not forwarded as
execution cancellation timers. Outbox delivery continues until receiver durable
acknowledgement; a broker flush or socket write is not acknowledgement. Retain
command receipts, attempt tombstones, update IDs and sequence identity records
for the runtime generation's entire life and at least seven days after verified
termination and credential revocation. Do not evict them during a long task.

## Context and progress

`agent` and `task` reuse public VCP schemas. A configured executor maps the
runner slug to an allowed implementation; settings cannot select arbitrary
host programs. Context is assembled by VCP through application ports before
acceptance. The payload contains task instructions/non-secret configuration and
references, not copied repositories or serialized agent conversations.
`working_directory` and `instruction_files` are workspace-root-relative paths;
reject absolute paths, traversal, symlink escapes and missing paths. References
are named immutable artifacts with a SHA-256 digest; resolve only authorized
schemes/storage, verify bytes, and never interpret a URI as a shell command.
Session continuation uses the daemon's durable session-to-executor mapping;
missing conversation state is a rejection, not an implicit new conversation.

`runtime.ready` registers the supported runner slugs, progress kinds and
concurrency limit after recovering the existing journal/executors. It records
execution readiness internally, not a second public workspace lifecycle
transition. Unsupported execution/progress capabilities are never fabricated.
Inspect remains available during recovery; execute returns retryable
`UNAVAILABLE` until registration is committed. Cancellation remains responsive.

During automatic provisioning, the daemon may publish its first `runtime.ready`
before VCP commits workspace creation. For the owned current generation in the
`provisioning` state, VCP returns retryable `UNAVAILABLE`; the daemon retains and
retries the same update. A different generation still receives `STALE_RUNTIME`.
Provider readiness probes use inspect responsiveness rather than waiting for the
registration acknowledgement, avoiding a circular dependency on create commit.

Each progress update is one immutable record:

- `conversation`: a complete message or independently identified chunk, with a
  stable message ID; never revise the text under a previously used ID. VCP
  allocates the public session history sequence once on commit.
- `usage`: a **delta** for the supplied period, not an accumulated total. VCP
  adds it exactly once. Delta periods must not overlap within an attempt.
- `artifact`: public artifact metadata, with matching workspace and attempt
  IDs. Publish only after the referenced content is durably available through
  an authorized artifact port. A guest-local temporary path is insufficient.

The daemon journals progress before sending it. Keep command reception and
inspection separate from the executor loop. Bound concurrent executors by the
advertised limit and apply backpressure to progress producers when durable
outbox capacity is exhausted; never silently drop progress or terminal records.
If the journal cannot accept work, reject new execute with `UNAVAILABLE`.

## Updates, ordering and public projection

VCP processes an update only for a known provisioned binding, known attempt and
matching execute identity/cause (except `runtime.ready`, which has no attempt).
Unknown attempts receive `IDENTITY_MISMATCH`. Artifact bindings, usage periods
and public cause references are also cross-checked against durable state.

VCP atomically stores update identity/digest and advances its contiguous runtime
sequence with all resulting application state and public outbox events before
replying `committed`. Duplicate event ID plus identical body returns committed
without repeating state/history/usage. Changed data under an event ID, or a
sequence already occupied by another ID, is `EVENT_CONFLICT`. A gap is
`EVENT_GAP`, with `expected_sequence`; replay from that point before proceeding.
The daemon transmits the oldest unacknowledged update first and retains
acknowledged events for recovery retention. Public event IDs are persisted once
at projection and retained across publication retries. Public per-workspace
ordering is assigned by VCP, independent of the internal runtime sequence.

| Runtime update | VCP result |
| -- | -- |
| runtime.ready | execution readiness/capabilities; no public task event |
| attempt.started | attempt running, session active; execute operation succeeds; task.started |
| attempt.progress conversation | session history entry and conversation.message |
| attempt.progress usage | accumulated usage and usage.reported |
| attempt.progress artifact | artifact record and artifact.created |
| attempt.completed | attempt completed, session idle; task.completed |
| attempt.failed | attempt failed, session idle; task.failed; execute operation fails only if never started |
| attempt.cancelled | attempt cancelled, session idle; task.cancelled; winning cancel operation succeeds |

Every terminal update confirms executor termination (or that launch never
occurred). A failed task does not fail its workspace. No progress or second
terminal outcome is allowed after a terminal record. VCP maps invalid state
transitions to `EVENT_CONFLICT`, quarantines the runtime and reconciles; it must
not silently skip a corrupt sequence to make later messages apply.

## Cancellation races

Cancel identifies one attempt, its session/runtime generation and original
execute identity, plus the cancel operation/cause. Acceptance journals a cancel
intent and replies promptly; it does **not** emit task.cancelled. If execute has
not arrived, journal a cancellation tombstone for that exact binding before
replying. A delayed matching execute receives accepted but is never launched;
conflicting execution identity receives ATTEMPT_CONFLICT. The tombstone produces
one cancelled update with termination `never_started`. VCP fails the execute
operation with `INVALID_STATE` (cancelled before start), while cancel succeeds.

If running, request graceful termination through the executor port, escalate
after its configured grace period, reap the process tree or obtain an equally
strong executor termination acknowledgement, and only then journal cancelled.
If termination cannot be confirmed, remain cancelling and report via inspect;
do not unlock the workspace for destruction. A timeout is not proof of death.

Completion and cancellation serialize in the daemon journal. If the attempt
already completed/failed, it keeps that outcome; cancel is accepted as a no-op.
VCP obtains the existing terminal outcome through replay/inspect and completes
that cancel operation with final_state completed/failed. If cancellation won,
task.cancelled carries the winning cancel cause; later cancel operations finish
from the same cancelled outcome without creating more events. Pending cancel
operations on one attempt may be coalesced by VCP but must all reach a terminal
operation result. The public event's `operation` references execute, while
`cancelled_by` and its causal path reference the winning cancel operation.

## Restart and uncertainty

VCP restart replays its command outbox and consumes duplicate updates safely.
It can inspect up to 100 specific attempt IDs per request; an empty list probes
readiness. A response includes exactly one snapshot per requested ID (`known:
false` if absent), with a consistent journal high-water sequence. Inspect is
read-only: it does not launch, retry or cancel work. Snapshots are reconciliation
evidence, not replacements for missing lifecycle events. Replay committed
journal updates to VCP before closing operations based on a snapshot.

Daemon restart acquires exclusive journal ownership, replays pending updates,
and reconciles executor identity/launch intent before advertising ready with a
new boot ID and the same journal ID. A recorded but never-launched attempt may
start once. A launching/running attempt must be reattached using a durable
executor handle/process identity including its creation identity, not a reused
PID. **Never automatically launch again when the outcome is uncertain.** If
reattachment is impossible, terminate/fence the old executor, confirm it cannot
continue, and emit failed with reason executor_lost. Until confirmation, remain
unready with the attempt launching/running/cancelling and do not permit destroy.
If compute is lost, VCP must establish provider termination/fencing before
synthesizing failure and releasing active-attempt ownership. Silence alone is
insufficient. A lost journal cannot be reconstructed as empty: retire/fence that
runtime and fail its attempts; never rerun them under their old IDs.

Examples are independent cases: completed, failed and cancelled illustrate
alternative outcomes, not three outcomes of the same attempt. The identical
execute/execute-retry pair illustrates response loss. Stale-runtime and event-gap
examples illustrate fencing and replay. Tests validate wire shapes and stateless
cross-field invariants; the stateful rules above require daemon/VCP integration
and crash tests in subsequent implementation steps.

## Validation

From `v2/`:

```sh
uv run python -m openengine.contracts.runtime
uv run pytest packages/contracts/tests/test_runtime_contract.py
cd packages/contracts/tooling
npm ci --ignore-scripts
node validate.mjs internal/workspace_runtime
```

Schema-invalid messages without a usable command/event identity and binding
cannot receive a typed acknowledgement. Discard them without side effects and
log a bounded diagnostic; well-formed but rejected messages receive the typed
error reply. Stateful validation (credentials, session ownership, generation
registry, journal sequences and executor termination) must run in the services;
the schema validator does not establish these facts.

## Reserved steering

`oe.internal.v1.runtime.runtimes.{runtime_id}.steer` and update kinds
`steering.applied` / `steering.failed` are reserved, with no send operation or
accepted wire schema in this release. Do not publish them or grant daemon steer
permissions yet. A later contract slice will define active and idle session
steering, durable acknowledgement and application semantics. Public VCP steering
responsibilities remain unchanged.

## Design provenance

Adapted only from the protocol and validation evidence in PR #1045 at
`891491c936e7078e8f10619566185d6150b942c7`, for issue #1116. Shared VCP types
and the single `openengine.cc/v1alpha1` resource API group from #1043 remain
authoritative. No public contract, handler or persistence behavior is changed.
