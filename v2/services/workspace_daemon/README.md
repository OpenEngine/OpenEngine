# Workspace daemon

One durable daemon process per runtime generation. It receives VCP's internal
workspace-runtime commands
([contract](../../packages/contracts/internal/workspace_runtime/v1/README.md)),
journals them, runs them through an executor port and sends ordered updates
back until VCP commits each one. It is supervised separately from VCP, beside a
Git worktree or inside a VM; nothing in it depends on where it is placed or
imports VCP code (`tests/test_boundaries.py`).

```sh
uv run --project v2 python -m workspace_daemon --nats-url nats://127.0.0.1:4222 \
  --journal-directory /var/lib/oe/rt_x --workspace-root /work/ws_x \
  --workspace-id ws_x --runtime-id rt_x --generation 1 --initialize --local-test-account
```

The deterministic `fake` executor runs no agent, model or ACP subprocess:
`agent.settings` may set `outcome` (`completed`/`failed`), `delay_seconds`
(0-60) and `ignore_cancel` (work that only stops when fenced at the
cancellation deadline). `--fake-effects` records each launch, as test evidence. The real
`codex` executor is opt-in; see "Codex ACP executor".

## Start-up and recovery

1. The workspace root and journal directory are configured separately and
   resolved; neither may contain the other, and the journal directory must be
   private (`0700`). Execute context paths must resolve, symlinks included,
   inside the root, so task context cannot name the journal.
2. An exclusive `flock` on the journal directory gives one process ownership
   until it exits.
3. `binding.json` seals an established binding. Without it, first setup needs
   `--initialize` and no existing journal, and records `initializing.json`
   before migrating, so an interrupted setup resumes for the same binding only.
   An established binding with a missing, schema-less, unreadable or rebound
   journal, or without its runtime record, is refused (exit 2); history is never
   recreated.
4. Alembic (`migrations/`) creates or upgrades the runtime-local SQLite journal
   (`workspace-daemon.sqlite3`). It is not a tenant service database. Its
   tables are normalized and related by foreign keys (see "Journal schema").
5. Recovery fences every `launching`, `running` or `cancelling` attempt through
   its executor and records it failed with `executor_lost` (`killed`), or
   cancelled when a cancel intent was journaled; it never launches it again. If an executor cannot confirm termination, start-up is refused with
   the attempt left as it was. A `recorded` attempt launches once. Each decision
   is logged with the command ID, runtime generation, journal ID and sequence.
6. A new `runtime.ready` (new `boot_id`, same `journal_id`) is journaled; the
   binding is sealed; only then are `.execute`, `.cancel` and `.inspect` subscribed, using
   the runtime's own inbox prefix.

The daemon accepts execute only after VCP commits its latest `runtime.ready`.

## Journal schema

| Table | Holds | Relations and constraints |
| -- | -- | -- |
| `journal_runtime` | the single binding (workspace, runtime, generation), `journal_id`, update high-water mark, readiness, blocked reason | one row (`id = 1`); `ready_sequence`, `blocked_sequence` → `journal_updates` |
| `journal_commands` | each remembered execute or cancel command's exact bytes, digest, and reply status and error code/message | `status` is `accepted` or `rejected`; an error exactly when rejected |
| `journal_sessions` | sessions this runtime owns, and the executor conversation (ACP session ID) and whether it can be loaded | `command_id` → the opening command, which carries the agent |
| `journal_attempts` | state (including `cancelling`), launch count, attempt sequence, last update sequence, executor handle | `session_id` → sessions (NULL only for a cancel-before-execute tombstone), `command_id` → the execute or tombstone's cancel command (unique), `cancel_command_id` → the winning cancel; one unfinished attempt per session |
| `journal_updates` | ordered update bytes as sent and whether VCP committed them | `attempt_id` → attempts for attempt updates |

Nothing is stored twice: a reply is rebuilt from its command's target and its
status columns, a session's agent is read from its opening command, and the
active-attempt count and busy sessions come from a partial index over
unfinished attempts. Protocol messages stay exact bytes, and the executor
handle stays opaque JSON that only the executor port reads.

## Commands, execution and updates

Execute checks the subject and binding against the provisioned one before any
receipt lookup (`IDENTITY_MISMATCH`, `STALE_RUNTIME`; nothing is journaled).
An identical retry returns the stored reply; changed content is
`COMMAND_ID_CONFLICT`; another command for a known attempt `ATTEMPT_CONFLICT`;
an unknown runner `UNSUPPORTED_EXECUTOR`; a busy session `SESSION_BUSY`; a bad
context or setting, or continuing a session whose executor conversation is
missing or not loadable, `INVALID_ARGUMENT`. Acceptance and those terminal
rejections are remembered; `UNAVAILABLE` (not ready, blocked, at capacity) is
not. Acceptance journals the receipt and a `recorded` attempt in one
transaction: the reply means durable ownership, not that work started.

The execution worker commits `launching`, `launch_count` and the executor's
planned launch identity (`executor_handle`) before the executor's side effect,
then `running`, `attempt.started` and any executor conversation identity, then
progress, then the terminal update. An exception from launch or wait is not
evidence that nothing started or that work stopped: only an executor's
confirmed `LaunchFailed` or returned outcome ends an attempt directly.
Otherwise the attempt stays `launching`/`running` (holding its session and
capacity) and the worker fences it with backoff, recording `executor_lost`
(`killed`) only after termination is confirmed. The same applies when the
terminal record itself cannot be journaled. Nothing is executed again.
Progress waits while 1000 updates are unacknowledged (backpressure reaches the
agent through its stdout pipe); terminal updates never wait and nothing is
dropped. Every update is validated against the contract and stored
with its global sequence and attempt sequence before it is sent. The outbox
sends the oldest unacknowledged update's exact bytes (shared `Client.send`,
one 5 s attempt, jittered backoff) and keeps them after VCP commits. `EVENT_GAP`
reopens retained updates from the requested sequence; any other permanent
rejection blocks the outbox and new execution for reconciliation. Inspect uses
a read-only connection and its own subscription, so it answers during
execution and recovery.

## Cancellation

Cancel is checked against the binding like execute, has its own subscription
and is never refused for readiness. Its receipt and the attempt's cancel
intent commit in one transaction before the reply, which means the intent is
durable, not that work stopped:

- No attempt yet (cancel overtook execute): a tombstone attempt for the exact
  binding and execution is journaled and immediately `attempt.cancelled`
  (`never_started`). A matching execute delivered later is acknowledged and
  remembered but never launched or opened; a different execution for that
  attempt is `ATTEMPT_CONFLICT`, as is a cancel naming another execution.
- `recorded`: cancelled `never_started` in the same transaction; the worker
  can no longer launch it.
- `launching`/`running`: becomes `cancelling`, keeping its session and
  capacity. A launch that returns afterwards still journals `attempt.started`
  but stays `cancelling`.
- Already completed or failed, or already cancelling: accepted as a no-op.
  Cancel and completion serialize in the journal; once an intent is journaled
  every terminal record of the attempt is `attempt.cancelled` with the first
  (winning) cancel's command ID and cause.

The worker stops a `cancelling` launch in two steps. It asks the executor to
stop (`cancel`; Codex sends ACP `session/cancel`, or skips a prompt not yet
sent) and waits for the work to end, both under one deadline (5 s) that also
covers acquiring the ACP write lock and draining stdin, so a blocked pipe or a
full progress outbox cannot hold it. Then it cancels the attempt's task, whose
executor fences the process tree. A fence already running is never interrupted;
the deadline restarts after it, so final progress blocked on a full outbox is
cancelled too and the attempt is fenced again (idempotently) and recorded. The
terminal record is journaled only after
a confirmed fence; failed fencing, an exception or a failed terminal write
leaves the attempt `cancelling` and owned, and fencing is retried with backoff.

## Codex ACP executor

`acp.py` is a small direct ACP v1 client (newline-delimited JSON-RPC over the
adapter's stdio; no LangGraph or langgraph-acp dependency). `codex.py` runs one
operator-configured adapter behind the same executor port as `fake`:

- The command chooses no executable, environment or UID, and Codex accepts no
  `agent.settings`. `agent.model` is set only if the adapter offers it as a
  `model` config option; otherwise launch fails before any prompt.
- Launch resolves `working_directory` and `instruction_files` again beneath the
  workspace root (symlinks included) and reads instructions without following
  any symlink. The prompt is the instructions, task and task inputs.
- The client advertises no filesystem or terminal capability, refuses client
  methods, selects the adapter's `read-only` mode (launch fails without one) and
  denies every permission request (a `reject_*` option, else `cancelled`).
- `session/new` opens a conversation; its ID and the adapter's `loadSession`
  capability are journaled with `running`. `continue` uses `session/load` on
  that ID and fails (rejection or `launch_failed`) if it is missing or cannot
  be loaded, never opening a new one. History replayed during load is discarded.
- Streamed agent message chunks are coalesced into `conversation` progress:
  one message (new message ID) per 32,000 characters and one for the rest of
  the turn. The completion summary keeps only the last 64 KiB of text. The prompt result's explicit
  `inputTokens`/`outputTokens` (and `cachedReadTokens`) become one `usage`
  delta for the turn. Cumulative `usage_update` notifications, costs and
  artifacts are never reported.
- Every prompt read and write shares a deadline (`--prompt-timeout-seconds`,
  default 3600), so an adapter that stops reading or answering fails the
  attempt. The whole process tree is fenced before any terminal update,
  including after `end_turn`. Only then are the turn's remaining text (also
  after a failed turn) and its usage journaled, so waiting for outbox capacity
  never keeps the agent alive past its deadline; a cancelled wait fences and
  skips that journaling. If that journaling fails the attempt fails
  (`executor_failed`) rather than completing with output or usage missing. Any other error during the turn fails the fenced
  attempt as `executor_failed`.

`supervision.py` is the only supervisor the installed daemon has. The daemon
runs as root on Linux; each launch gets a cgroup under a root-owned delegated
cgroup-v2 directory, named in the journaled handle before spawning. The trusted
launcher (`launch.py`, `python -I`) joins it, drops to the agent UID/GID with
`no_new_privs`, and executes the adapter with only `PATH`, `HOME` and `LANG`.
Fencing writes `cgroup.kill` until `cgroup.events` reports `populated 0`, then
removes the cgroup, so a launcher that had not joined can never join later; a
handle from an earlier boot is already fenced. A timeout returns unconfirmed
and the attempt stays owned. Start-up refuses the codex executor (exit 1) off
Linux, without root, without a delegated cgroup root, or if the agent account
owns or can write the cgroup root, journal directory, workspace root, the
resolved launcher or `--codex-acp` adapter, or any directory above either.

Where OS enforcement stops: those checks cover only the paths themselves, not
the trees beneath them, ACLs or the journal database file. Read-only execution
is otherwise the ACP `read-only` mode plus operator file permissions; anything
inside the workspace the agent account owns or can write, its HOME and `/tmp`
stay writable to it. Every attempt on a host runs as the one configured agent
UID, so concurrent attempts are not isolated from each other (signals,
`/proc`, the shared HOME and Codex configuration); the cgroup bounds lifetime,
not isolation. Run one attempt per agent account where that matters.

Operator provisioning (not done by the daemon or VCP): install the Codex ACP
adapter at an absolute path; create the agent account and authenticate Codex
in its HOME; make the workspace readable but not writable by that account;
delegate a root-owned cgroup-v2 directory with `cgroup.kill` (Linux 5.14+).

```sh
oe-workspace-daemon … --codex-acp /usr/local/bin/codex-acp --cgroup-root /sys/fs/cgroup/oe \
  --agent-uid 990 --agent-gid 990 --agent-home /home/oe-agent
```

Tests: `tests/test_codex.py` (and VCP's `test_runtime_progress.py`) run a
deterministic ACP subprocess fixture (`tests/acp_agent.py`) behind a test-only
process-group fence (`tests/acp_fixture.py`, never installed) on macOS and
Linux. They cover context, continuation after restart, permission denial, model
and mode capabilities, progress, usage and backpressure, blocked I/O, adapter
crash, restart mid-prompt, unconfirmed fencing, failed terminal journaling and
launch/wait exceptions; `tests/test_cancellation.py` covers tombstones,
recorded/launching/running cancellation, graceful ACP cancel, blocked stdin,
backpressure, failed fencing and terminal writes, and restart with a
journaled intent. They are not evidence of cgroup fencing or of an
authenticated model. `tests/test_linux_supervision.py` is opt-in: as root on
Linux with `OE_DAEMON_TEST_CGROUP_ROOT`, `OE_DAEMON_TEST_AGENT_UID`,
`OE_DAEMON_TEST_AGENT_GID` and `OE_DAEMON_TEST_AGENT_HOME` it fences a real
process tree; with `OE_DAEMON_TEST_CODEX_ACP` and `OE_DAEMON_TEST_WORKSPACE` it
also runs one live, authenticated Codex task. Both skip otherwise.

## Limits

Production credentials are provisioned out of band (`--credentials`); the
shared local account behind `--local-test-account` is a test fixture.
Real-agent steering, approvals, writable execution, artifacts, other
runners, credential enrollment, macOS host fencing, VM installation and
suspend/restore are later work (#1122, #1055 and their successors). Without
`--codex-acp` only `fake` runs; there is no unsupervised real executor. Beside a worktree, VCP's
worktree adapter starts and stops this process (see the VCP README).


## Fake steering (#1121)

Readiness advertises `steering_executors: ["fake"]`. A steer command must name the
exact running attempt, session, execution and runtime generation. The journal
transaction remembers its receipt, appends the message to the fake executor's
persisted `executor_handle` and journals `steering.applied` together. The existing
opaque handle column needs no daemon schema change. A stopped or cancelling
attempt records `steering.failed` with no task-result change. Duplicate commands
replay receipts; conflicting IDs reject. Codex always rejects steering, even if
advertised incorrectly, with no fallback to cancellation or another turn.

Completed fake results include applied messages in journal order, bounded by the
public summary limit (a digest covers any truncated tail). Restart fences an
interrupted fake attempt as before and replays committed updates; it never
reapplies steering or targets a new attempt. `tests/test_steering.py` covers
transaction rollback/retry, binding and capability rejection, terminal races,
duplicate delivery and maximum-length messages with deterministic fake execution.
