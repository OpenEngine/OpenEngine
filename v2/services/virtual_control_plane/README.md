# Virtual Control Plane

The first V2 milestone implements durable workspace creation, ensure for an
available runtime, destruction, capabilities, workspace get/list and operation
get over the existing `oe.v1.vcp` NATS contracts. Step 2 adds durable execute
acceptance and session get/list queries with attempt snapshots. Step 3 delivers runtime commands to a standalone daemon with a fake executor.
Step 4 adds an opt-in Codex ACP executor. Step 5 adds durable cancellation.
Approvals, checkpoints and suspension remain later milestones.

## Run locally

From `v2/`, run `uv sync`. `oe up` always starts VCP after every other service;
set `OE_VCP_SMOLVM_IMAGE` first to register the smolvm provider, or leave it
unset to start with no providers configured:

```sh
OE_VCP_SMOLVM_IMAGE=your-python-and-git-image uv run oe up
```

The supervisor starts it after NATS, waits for its loopback HTTP readiness
endpoint, records its PID/endpoint, and stops it before NATS. `oe down` retains
workspaces and VMs. Use `workspaces.destroy` to remove compute explicitly.

For standalone operation:

```sh
uv run oe-vcp --nats-url nats://127.0.0.1:4222 --local-account --image your-image
```

The image must contain `python3`, `sleep`, and every requested tool (`terminal`
means `sh`). Repository context additionally requires Git. Provisioning places
repositories under `/workspace/repositories/0`, `/1`, etc., context files under
`/workspace`, and standing instructions in `/workspace/AGENTS.md`. Preparation
checks paths, verifies tooling and writes a completion marker. It can resume
before that marker is written. No host directories or credentials are mounted
or forwarded. The smolvm CLI is a host prerequisite.

Without an image VCP serves queries but advertises no configured providers.
The service applies its Alembic migrations and creates the existing contract's
`vcp-events` stream if absent. Existing stream settings are managed by
`oe migrate`. `OE_SQLITE_DIRECTORY` chooses the durable data directory.

## Boundaries and durability

- `domain/` defines workspace rules, IDs and event construction.
- `application/commands/` and `application/queries/` depend on the small
  interfaces in `application/ports/`; they import no NATS, SQLAlchemy or
  provider implementation classes.
- `transport/nats/` validates subjects, JSON Schema and causal paths, maps
  native replies, and publishes events with JetStream acknowledgement.
- `persistence/durable.py` implements a transaction interface with SQLite
  `BEGIN IMMEDIATE`. The generated Alembic revision adds command receipts,
  operations, workspace documents, runtime ownership, cursors and outbox
  records. The old prototype workspace table remains for compatibility;
  its records are not automatically adopted as managed runtimes.
- `workers/` runs one operation at a time and publishes outbox events in
  workspace version order. SQL transactions do not span provider calls.
- `providers/smolvm/` reconciles deterministic, persisted machine names and
  verifies ownership labels before operating on existing compute.

Acceptance atomically records the command response, workspace, ownership,
operation and initial event. Matching retries return the original response;
conflicting command reuse fails. The deadline applies only before acceptance.
Terminal rejections are retained; `UNAVAILABLE` is retried without a receipt.
Authorization failures do not reserve IDs belonging to another actor.
Receipts and cursors are retained indefinitely in this milestone, exceeding
contract minimums of seven days and 24 hours respectively. A retention/GC
policy is still needed before high-volume deployment.

A process lock permits one VCP per SQLite directory. This is a local,
single-writer implementation, not an HA deployment. The worker resumes
`pending` and `running` operations after restart using their existing runtime
ownership. Smolvm provisioning finds an existing owned machine before creating
one; destruction confirms absence. Failure retains ownership for cleanup.
Provider errors are visible on operations; a failed ensure leaves workspace
state unchanged. Provisioning/destruction failures mark the workspace failed.
A new destroy command can clean up a partially provisioned or destroyed VM.
If a provider is no longer configured after restart, its accepted operation
fails explicitly with `PROVIDER_ERROR` and releases the workspace operation
lock. Other queued work can proceed. Ownership is retained; restore the provider
configuration and submit a new command for cleanup or ensure. Replaying the old
command still returns its original acceptance and terminal operation.

Outbox publication is at least once. Events retain IDs and causal paths across
retries; consumers must deduplicate beyond JetStream's duplicate window.
Publication failures leave records pending. Query cursors are durable, opaque,
filter-bound keyset positions. Current document queries scan their collections;
indexed projections and outbox compaction are future scaling work.

## Explicit protocol and deployment decisions

NATS does **not** attach the publisher's authenticated identity to a subscriber's
message. `--local-account` explicitly trusts all publishers in a dedicated
single-tenant local account and assigns them the local actor. The supervisor
only enables this on loopback. Without that flag authorization denies access.
Do not use it for untrusted shared accounts: verified delegation and broker ACL
provisioning must precede a multi-tenant deployment. Payload fields and arbitrary
headers never establish actor identity.

The contract requires command IDs even in error replies, but does not define a
reply when a malformed request contains no valid ID. Such responses are best-effort errors (`cmd_invalid` when absent); clients must fix the envelope rather
than rely on retry identity. Valid IDs with schema-invalid payloads have durable
rejections. Requests without a reply inbox are ignored.

The public command retry identity explicitly names subject, payload,
`expected_version` and `causal_path`; `deadline_at` is consequently excluded.
Ready-workspace ensure completes its operation without changing the workspace
or inventing an additional ready event. Suspended-workspace restoration is
explicitly unsupported. Destroy requires both retention choices and rejects
active attempts. No artifacts/checkpoints exist in this milestone, so either
retention choice applies to an empty set; compute storage is removed.

Smolvm advertises microVM isolation and configured tool support, but no
checkpoint/suspend/restore capabilities. Resource maxima are VCP admission
limits; host availability can still cause provisioning to fail. CPU limits must
be whole vCPUs; fractional requests are explicitly rejected. This implementation
uses the installed released CLI's named-machine create/start/exec/list/delete
operations, not V1's disposable context manager.

## Verification

```sh
uv run pytest services/virtual_control_plane/tests
NATS_SERVER=/path/to/nats-server uv run pytest integration_tests/test_vcp.py
OE_VCP_TEST_IMAGE=python:3.12-alpine NATS_SERVER=/path/to/nats-server \
  uv run pytest integration_tests/test_vcp.py -k real_smolvm
```

Unit tests cover fake storage and compute, migrated SQLite, concurrent command
retries, durable rejections, deadlines, versions, authorization, lifecycle
constraints, process exclusion, cursor restart recovery, provider failure and
lost publication acknowledgements. Cross-service tests launch separate VCP and
real NATS processes and assert only public requests and events. The fake compute
fixture can terminate VCP after allocation to verify recovery. The opt-in real
smolvm test creates, rediscovers after VCP restart, ensures and explicitly
destroys a VM.

## Durable execution acceptance (Step 2)

Execute requires a ready workspace with a running, owned runtime and an injected
execution-context port. Acceptance saves the receipt, pending operation, active
session, starting attempt, runtime-generation binding and one internal execute
outbox entry in the same transaction. Continuing an active session is rejected;
separate sessions may have concurrent attempts. Queries expose attempts through
session snapshots. Matching retries preserve the original IDs and wire command,
including after a process restart. Infrastructure workers leave execution
operations pending.

Tests inject a fake context port and exercise the real internal command encoder.
Execution is disabled by default. `--fake-executor` enables acceptance after a
daemon registers the fake runner. Without that option (or an injected context
port), execute returns `UNSUPPORTED_CAPABILITY`.
Destroy rejects workspaces with active attempts. A runtime acknowledgement alone
does not complete an execute operation: the daemon started update does so.

## Workspace daemon and delivery (Step 3)

Run VCP with `--local-account --fake-executor` in an isolated local test NATS
account. Provision the daemon inside the workspace runtime with the IDs returned
by workspace get. Install this package and its dependencies in the runtime image;
the smolvm provider does not yet install packages or broker credentials for you.
The daemon connects to a broker address reachable from that runtime:

```sh
oe-workspace-daemon --nats-url nats://BROKER:4222 \
  --workspace-id ws_ID --runtime-id rt_ID --generation 1 \
  --workspace-root /workspace --journal-directory /var/lib/oe-runtime \
  --credentials /run/secrets/runtime.creds --initialize
```

Use `--initialize` for first provisioning; omit it on restart. First setup saves
an atomic `initializing.json` intent before migration and executor validation.
Interrupted setup can resume with the same binding, with or without the flag.
Only after journal recovery commits does startup write the durable `binding.json`
marker, before opening any command listener. Established bindings always require
existing journal metadata, even if `--initialize` is repeated or an old pending
marker survives. Missing established journals and mismatched bindings fail
startup. Legacy binding markers without metadata remain ambiguous and require
explicit fencing/replacement; they are never silently recreated.
The journal directory must be on
persistent runtime storage, separate from VCP's database and writable task
context. An exclusive process lock covers recovery, reception and executor
cleanup. For isolated test accounts, `--local-account` replaces credentials.
See the internal contract for per-runtime command and reply-inbox permissions.
VCP's current service supports the explicit local-account trust mode only;
credential provisioning and authenticated multi-tenant deployment remain separate.

`execute` uses runner `fake`, with optional agent settings `outcome` (`completed`
or `failed`) and `delay_seconds` (0–60, default 0.05 per phase). The daemon checks
workspace-relative paths, missing files, symlink escapes and session ownership.
It simulates started, one conversation progress message, then a terminal result.
It advertises only conversation progress; usage and artifact producers are not
implemented. No agent, shell command or child process runs.

Commands retain their original bytes and IDs until durable acceptance. Both
senders use a five-second request timeout and exponential jittered retries capped
at ten seconds. The daemon publishes its oldest unacknowledged update first and
retains committed entries for gap replay. VCP commits deduplication, contiguous
sequence, attempt/session/operation state and public outbox events atomically.
The execute operation succeeds on started; task failure later leaves it succeeded.
A pre-start failure fails that operation. Terminal outcomes release the session
and active-attempt workspace lock, allowing destroy. Conflicts quarantine updates
or block command delivery for reconciliation; they never imply executor death.

A recovered `recorded` attempt can launch once. A `launching` or `running` fake
attempt becomes failed with `executor_lost` after exclusive ownership confirms the
old daemon exited. This is safe specifically because the fake executor lives in
that process. Real executors will require durable handles and confirmed process
tree termination or reattachment before releasing ownership. The daemon resumes
reception independently of fake execution; inspect remains responsive. New work
waits until its recovered runtime.ready update is committed. Progress capacity is
reserved at admission for the fake executor's bounded three-update lifecycle.

Recovery tests cover both lost acknowledgements, unchanged retries, journal
conflicts, generation fencing, ordered gap replay, uncertain launch intent and
process death. Real NATS tests run VCP and daemon in separate processes, restart
both, verify terminal results and public events, replay execute, and destroy the
workspace. The optional smolvm infrastructure test does not exercise guest daemon
installation. The cancellation and ACP sections below describe agent termination
and recovery.

## Codex ACP executor (Step 4)

This implementation uses a small direct ACP v1 JSON-RPC client, with no
`langgraph-acp` or LangGraph dependency. Both fake and Codex executors implement
the daemon's executor port; the supervisor owns launch intent and retryable
outcome reconciliation. Codex runs in **read-only mode**. Permission requests
are denied, and client filesystem/terminal capabilities are not advertised.
There is no approval workflow in this milestone.

Enable acceptance with VCP's `--codex-executor`. A registered daemon must advertise
`codex` before requests can be accepted. The context port derives the working
directory from the persisted workspace specification (`repositories/0` when a
repository was provisioned, otherwise `.`), includes provisioned `AGENTS.md`, and
delivers task inputs. The daemon checks paths again and opens instruction files
without following symlinks. Arbitrary agent settings and artifact references are
rejected. The public `model` field is applied only through an advertised ACP model
configuration option. Commands cannot choose an executable, environment or UID.

The real executor requires a Linux guest with cgroup v2 and `cgroup.kill`, a
root-owned delegated cgroup directory, a root daemon, and a separate unprivileged
agent account. Provision the adapter and its agent dependencies in the guest,
with credentials in that account's home. No credentials are transported through
VCP or copied from the host. The task directory must be accessible to that UID.
Keep the journal and cgroup hierarchy inaccessible for writing by the agent.

Example additions to the daemon command from Step 3:

```sh
--codex-acp /usr/local/bin/codex-acp \
--cgroup-root /sys/fs/cgroup/oe-runtime \
--agent-uid 1000 --agent-gid 1000 --agent-home /home/agent
```

The adapter must expose ACP v1, read-only mode and (for continuation) `loadSession`.
Authentication must already work for the guest account; the daemon never runs a
login flow. Installation, credentials, delegated cgroups and VM startup remain
operator provisioning responsibilities. No npm install or network download runs
on the task acceptance path.

A unique cgroup name and host boot identity are saved in the existing attempt
journal document **before launch**. The trusted launcher joins that group, drops
supplementary groups and UID/GID, then execs the operator-configured adapter with
a minimal environment. Agent tools inherit the cgroup. Completion and recovery
kill the group, confirm it empty and remove it before releasing ownership. A
late launcher cannot enter a removed group. This uses
[Linux cgroup v2 fencing](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
Recovery does not reattach or repeat an interrupted prompt: it fences the old
process tree and emits `executor_lost`. Missing fencing configuration or an
unconfirmed stop keeps the attempt owned and prevents readiness. Existing
`recorded` work whose executor was removed fails before launch.

ACP session IDs and resume capability are stored in the existing session journal
document. Continuation calls `session/load`; missing/unloadable conversation state
fails instead of creating a replacement. History replay during load is excluded
from live progress. Each live text chunk gets a new durable message identity.
The client waits for journal capacity, applying backpressure through the pipe.
Only explicit prompt-result input/output token counts are projected as one usage
delta; cumulative `usage_update` notifications are not summed. VCP deduplicates
usage and rejects overlapping periods. No artifacts or cost estimates are
fabricated: artifact publication awaits an authorized durable artifact port.

Tests use a separate ACP subprocess and real NATS, including context delivery,
permission denial, session continuation, progress/usage projection, duplicate
updates, restart and execution fencing failures. The fixture's childless process
fence exists only in tests and cannot be selected by the deployed CLI. Linux
process-tree and live-model tests are opt-in:

```sh
export OE_VCP_TEST_CGROUP_ROOT=/sys/fs/cgroup/oe-runtime
export OE_VCP_TEST_AGENT_UID=1000 OE_VCP_TEST_AGENT_GID=1000
export OE_VCP_TEST_AGENT_HOME=/home/agent
# Set only for a provisioned, authenticated adapter and a live model request:
export OE_VCP_TEST_CODEX_ACP=/usr/local/bin/codex-acp
uv run pytest services/virtual_control_plane/tests/test_live_codex.py
```

These tests are skipped on non-Linux hosts. Passing the protocol fixture tests
alone does not verify guest provisioning or live model authentication.


## Cancellation (Step 5)

Send `workspaces.<workspace_id>.cancel` with an `attempt_id` and optional `reason`.
Acceptance atomically saves the receipt, pending cancel operation, cancelling
attempt and immutable internal command. The attempt remains in the workspace's
active list and its session stays active until the daemon confirms termination.
Cancellation of an already terminal attempt succeeds immediately with its
existing outcome, without sending another runtime command or terminal event.

The daemon acknowledges after journaling intent, independently of executor work.
A cancel arriving before execute saves a durable tombstone and a
`never_started` cancelled update. A later matching execute is acknowledged but
never launched. The first cancel intent wins; subsequent cancellations share
the eventual terminal outcome. Completion committed before intent keeps its
original outcome. Both races are serialized in the journal transaction.

For ACP, the supervisor sends a `session/cancel` notification and gives the
executor two seconds to finish. That deadline includes waiting for the stdin
write lock and pipe drain. After the grace period it interrupts the executor
coroutine, whose cleanup kills and drains its cgroup. Prompt completion also
requires fencing. Failed fencing or a failed journal write retains ownership
and is retried; neither an acknowledgement nor a prompt response establishes
termination. Command reception and update delivery continue independently.
Recovery fences persisted launching/running/cancelling handles and resolves
cancel intent without repeating an uncertain launch.

VCP validates the winning cancellation against its persisted command, commits
the update identity and sequence, releases attempt/session ownership, completes
all pending cancel operations and enqueues one `task.cancelled` event in the
same transaction. The event references the original execute and winning cancel;
its causal path follows that cancel. Cancellation before start fails the pending
execute operation with `INVALID_STATE`. A completed execute operation stays
succeeded. No SQL schema changes are needed: cancellation intent and process
identities are fields in existing Alembic-created document tables.

Tests cover cancellation before delivery, recorded/launching/running work,
completion races, lost acknowledgements, repeated cancels, restart recovery,
failed fences, atomic acceptance rollback, and blocked ACP stdin. The real NATS
test uses the ACP subprocess fixture across VCP/daemon restarts and destroys the
workspace after cancellation. The opt-in Linux live-model test also checks that
the execution cgroup is gone; it requires the provisioning described above.

## Active-session steering

Public steer targets a session. Acceptance binds its current running attempt and
saves the receipt, operation and immutable runtime command together. It does not
mean the executor has applied the message. Outcomes complete the operation and
publish steering.applied or steering.failed, plus one session history entry.
Use query.sessions.<session_id>.events.list to read message and steering history
with kind/attempt filters and durable sequence cursors.

This first implementation supports active-attempt steering through the fake
executor. It stores the message in the attempt's durable executor state and
includes it in the fake result. Its effect and update commit in one transaction,
so retry or restart cannot apply the message twice. Runtime registration
advertises steering_executors; missing capability means unsupported. Starting,
cancelling and closed sessions cannot be steered. Idle next-turn steering and
suspended restoration are explicitly unsupported in this PR; the public contract
still permits them for implementations that support those capabilities.

The configured Codex backend does not advertise safe steering. No codex-acp
executable or pinned adapter version is available in this checkout's environment.
Source verification on 2026-10-22 found that the upstream adapter advertises
steering but may start a new turn when active-turn injection races completion:
[CodexAcpServer.ts](https://github.com/agentclientprotocol/codex-acp/blob/main/src/CodexAcpServer.ts).
That extension does not meet this implementation's bound-attempt and durable
effect requirements. VCP returns UNSUPPORTED_CAPABILITY before accepting a steer
for Codex. It never sends a concurrent prompt, cancels and reprompts, or silently
queues a future turn. Supporting a specific adapter requires a pinned, tested
turn-fenced/idempotent protocol and recovery for uncertain external application.

Completion or cancellation before application produces a failed steer without
changing the attempt outcome. Pending steering operations also resolve when VCP
commits attempt termination, so undelivered commands cannot strand operations
after destruction. A late duplicate failure is consumed without another event.
The daemon replays pending intents after restart, but fences interrupted attempts
first; it never retargets steering to a subsequent attempt or generation.

## Automatic local VM provisioning

Build a guest bundle from this checkout's V2 lockfile (no Docker required):

```sh
uv run --project v2 python -m virtual_control_plane.provisioning.bundle /tmp/oe-runtime-bundle --fixture
```

The bundle contains service/persistence wheels, hashed dependencies and a root
bootstrap. `--fixture` explicitly includes a deterministic ACP subprocess for
infrastructure testing; it does not call a model. Omit it for a real adapter.
Use a fresh destination when rebuilding. The base image must provide Python 3,
venv, useradd/groupadd and a writable cgroup v2 mount with cgroup.kill support.
Reserve UID/GID 1000 for oe-agent; conflicting accounts fail provisioning.
The integration test uses `python:3.12-slim`; pin an image digest for deployments.

With the public JetStream NATS server already running, start VCP with:

```sh
uv run --project v2 oe-vcp --local-account --image python:3.12-slim \
  --runtime-bundle /tmp/oe-runtime-bundle \
  --runtime-broker-binary /absolute/path/to/nats-server \
  --runtime-adapter /opt/oe/fixture
```

Create now installs the daemon, its private journal and credentials, an
unprivileged oe-agent account, and a root-owned execution cgroup. The persistent
VM workload supervises the daemon and starts it again when the VM boots. Create
waits for the daemon to answer inspect; execute admission separately waits for
VCP to commit runtime.ready. Bootstrap retries preserve the binding and journal.

VCP owns a separate authenticated internal broker. Each runtime can receive only
its commands and acknowledgement inboxes, and publish only its updates. User
credentials survive VCP restart and are revoked after confirmed VM destruction.
An inherited ownership lock and a dedicated supervisor tie broker lifetime to
VCP's control pipe. Even SIGKILL closes that pipe: the supervisor terminates and
reaps its broker before releasing the lock, and replacement VCP startup waits
for that cleanup. It never signals a PID read from disk. A fresh control-only
password prevents accidentally connecting to an older listener; runtime
credentials remain stable. Unmanaged listeners (including brokers started by
older versions without supervision) are rejected, not adopted or terminated.
Stop those listeners explicitly before starting VCP.

Startup has a ten-second deadline covering ownership acquisition and connection
attempts. Each initial connection attempt has a one-second outer deadline.
After connection, automatic reconnect continues across transient socket loss
and restores command reply inboxes and runtime-update subscriptions. The initial
deadline bounds NATS retries without disabling later reconnection.
Startup failure closes the control pipe and waits for owned
child cleanup; the supervisor allows three seconds for graceful broker shutdown
before killing and reaping it.

Its default port is 4422; `--runtime-port` changes it. The broker binds host
loopback; the guest reaches it through smolvm's default gateway at 100.96.0.1 because guest loopback is separate. Use
`--runtime-bind-address` to choose an interface and `--runtime-guest-url` to
override the guest endpoint. Keep those addresses and the port stable across
restarts; endpoint drift is rejected instead of silently stranding guests.

This is a single-operator local development setup: the internal connection is
authenticated TCP without TLS, and the public API retains its existing
local-account trust model. Do not expose it on an untrusted network. Guest
egress allows the broker host and PyPI download hosts. Add
`--runtime-egress-host HOST` for each required model API or repository host.
Smolvm host allowlists are host-wide, not port-specific.

For a real model, supply an image containing the configured absolute ACP adapter
and its authentication under /home/oe-agent, then set `--runtime-adapter` to that
executable. Host model credentials are never copied automatically. The existing
executor remains read-only; approvals and real active-turn steering remain
separate work. Daemon logs are in /var/lib/oe-runtime/daemon.log; broker state and
logs are private files under the VCP state directory's runtime-broker folder.
Log rotation and storage retention remain operational follow-up work.
