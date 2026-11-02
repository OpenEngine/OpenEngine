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

Only the deterministic `fake` executor exists. It runs no agent, model or ACP
subprocess: `agent.settings` may set `outcome` (`completed`/`failed`) and
`delay_seconds` (0-60). `--fake-effects` records each launch, as test evidence.

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
5. Recovery fences every `launching` or `running` attempt through its executor
   and records it failed with `executor_lost` (`killed`); it never launches it
   again. If an executor cannot confirm termination, start-up is refused with
   the attempt left as it was. A `recorded` attempt launches once. Each decision
   is logged with the command ID, runtime generation, journal ID and sequence.
6. A new `runtime.ready` (new `boot_id`, same `journal_id`) is journaled; the
   binding is sealed; only then are `.execute` and `.inspect` subscribed, using
   the runtime's own inbox prefix.

The daemon accepts execute only after VCP commits its latest `runtime.ready`.

## Journal schema

| Table | Holds | Relations and constraints |
| -- | -- | -- |
| `journal_runtime` | the single binding (workspace, runtime, generation), `journal_id`, update high-water mark, readiness, blocked reason | one row (`id = 1`); `ready_sequence`, `blocked_sequence` → `journal_updates` |
| `journal_commands` | each remembered execute command's exact bytes, digest, and reply status and error code/message | `status` is `accepted` or `rejected`; an error exactly when rejected |
| `journal_sessions` | sessions this runtime owns | `command_id` → the opening command, which carries the agent |
| `journal_attempts` | state, launch count, attempt sequence, last update sequence, executor handle | `session_id` → sessions, `command_id` → commands (unique); one unfinished attempt per session |
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
context or setting `INVALID_ARGUMENT`. Acceptance and those terminal
rejections are remembered; `UNAVAILABLE` (not ready, blocked, at capacity) is
not. Acceptance journals the receipt and a `recorded` attempt in one
transaction: the reply means durable ownership, not that work started.

The execution worker commits `launching` and `launch_count` before the
executor's side effect, then `running` with `attempt.started`, then the
terminal update. Every update is validated against the contract and stored
with its global sequence and attempt sequence before it is sent. The outbox
sends the oldest unacknowledged update's exact bytes (shared `Client.send`,
one 5 s attempt, jittered backoff) and keeps them after VCP commits. `EVENT_GAP`
reopens retained updates from the requested sequence; any other permanent
rejection blocks the outbox and new execution for reconciliation. Inspect uses
a read-only connection and its own subscription, so it answers during
execution and recovery.

## Limits

Production credentials are provisioned out of band (`--credentials`); the
shared local account behind `--local-test-account` is a test fixture.
Cancellation, steering, progress, real ACP executors, OS-level separation of
the workload's identity from the journal, VM installation and suspend/restore
are later work (#1122, #1055 and their successors). Beside a worktree, VCP's
worktree adapter starts and stops this process (see the VCP README).
