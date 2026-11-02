"""Run one runtime generation's workspace daemon as its own supervised process.

    python -m workspace_daemon --journal-directory J --workspace-root R \\
        --workspace-id ws_… --runtime-id rt_… --generation 1 [--initialize] [--ready-file F]

The same process serves a daemon beside a Git worktree or inside a VM: it
needs a workspace root, a separate private journal directory, its immutable
binding and a broker. Start-up takes the journal's exclusive lock, checks or
records the binding, migrates and recovers the journal, seals the binding and
only then subscribes to `oe.internal.v1.runtime.runtimes.{runtime_id}.execute`
and `.inspect`. Command reception, inspect, the executor worker and the update
outbox run independently. A refused recovery exits 2 with its evidence logged.

Broker credentials are provisioned out of band (`--credentials`). Without
them `--local-test-account` must be explicit: a shared unrestricted account is
a test fixture, never production authentication. The fake executor runs no
agent or model. The real `codex` executor is opt-in (`--codex-acp`) and needs
Linux cgroup-v2 supervision as root with an unprivileged agent account; if that
cannot be established the daemon exits 1 before recovery, never falling back to
unsupervised execution.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

import nats
from sqlalchemy.exc import SQLAlchemyError

from openengine.contracts.runtime import validate_wire_message
from openengine.messaging import Client
from openengine.persistence.migrations import current_revision

from workspace_daemon import store as journal_store
from workspace_daemon.codex import CodexExecutor
from workspace_daemon.executor import ExecutionWorker, ExecutorPort, FakeExecutor
from workspace_daemon.initialization import LOCK_FILE, Initialization, RecoveryRefused, Refusal, locations
from workspace_daemon.journal import Journal, encode
from workspace_daemon.outbox import UpdateOutbox
from workspace_daemon.supervision import CgroupSupervisor, SupervisionUnavailable, Unsupervised

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
REQUEST_TIMEOUT_SECONDS = 5.0
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

log = logging.getLogger("workspace_daemon")


class Updates:
    """Update delivery through the shared client's envelope-free `send`, one attempt per call."""

    def __init__(self, connection) -> None:
        self._client = Client(connection, timeout=REQUEST_TIMEOUT_SECONDS, attempts=1)

    async def send(self, subject: str, wire: bytes) -> bytes:
        return await self._client.send(subject, wire, name="oe.internal.v1.vcp.runtimes.{runtime_id}.updates")


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workspace_daemon", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nats-url", default=os.environ.get("OE_NATS_URL", "nats://127.0.0.1:4222"))
    parser.add_argument("--journal-directory", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--initialize", action="store_true", help="permit first setup of an empty journal")
    parser.add_argument("--max-concurrent-attempts", type=int, default=1)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--credentials", type=Path, help="provisioned runtime-scoped NATS credentials")
    parser.add_argument("--local-test-account", action="store_true")
    parser.add_argument("--fake-effects", type=Path, help="record each fake launch here (test evidence)")
    codex = parser.add_argument_group("codex executor (opt-in; Linux, root, cgroup v2)")
    codex.add_argument("--codex-acp", type=Path, help="absolute path of the provisioned Codex ACP adapter")
    codex.add_argument("--cgroup-root", type=Path, help="root-owned delegated cgroup-v2 directory for launches")
    codex.add_argument("--agent-uid", type=int, help="unprivileged account the agent runs as")
    codex.add_argument("--agent-gid", type=int)
    codex.add_argument("--agent-home", type=Path, help="the agent account's HOME, holding its provisioned authentication")
    codex.add_argument("--prompt-timeout-seconds", type=float, default=3600.0)
    return parser


def executors(args, journal_directory: Path, root: Path) -> dict[str, ExecutorPort]:
    """Configured executors; raises SupervisionUnavailable rather than run a real agent unconfined."""
    configured: dict[str, ExecutorPort] = {"fake": FakeExecutor(args.fake_effects)}
    if args.codex_acp is not None:
        if (None in (args.cgroup_root, args.agent_uid, args.agent_gid, args.agent_home)
                or not args.codex_acp.is_absolute() or args.prompt_timeout_seconds <= 0):
            raise SupervisionUnavailable(Unsupervised.CONFIGURATION)
        try:
            adapter = args.codex_acp.resolve(strict=True)
        except OSError as error:
            raise SupervisionUnavailable(Unsupervised.CONFIGURATION, str(args.codex_acp)) from error
        # A workload able to replace the adapter, or a directory above it, would run in every later task.
        supervisor = CgroupSupervisor(args.cgroup_root, uid=args.agent_uid, gid=args.agent_gid, home=args.agent_home,
                                      protected=(journal_directory, root, adapter, *adapter.parents))
        configured["codex"] = CodexExecutor(args.codex_acp, supervisor, prompt_timeout=args.prompt_timeout_seconds)
    return configured


async def serve(args, journal_directory: Path, root: Path, initialization: Initialization,
                configured: dict[str, ExecutorPort]) -> int:
    binding = {"workspace_id": args.workspace_id, "runtime_id": args.runtime_id, "runtime_generation": args.generation}
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopping.set)
    store = journal_store.JournalStore(journal_store.database(journal_directory))
    connection = worker = outbox = None
    subscriptions = []
    try:
        journal = Journal(store, binding, root, configured,
                          max_concurrent_attempts=args.max_concurrent_attempts, established=initialization.established)
        await journal.recover()
        initialization.finish()
        options = {"servers": [args.nats_url], "name": f"workspace-daemon-{args.runtime_id}",
                   "inbox_prefix": f"_INBOX.oe.runtime.{args.runtime_id}", "max_reconnect_attempts": -1}
        if args.credentials:
            options["user_credentials"] = str(args.credentials)
        connection = await nats.connect(**options)

        async def receive(message) -> None:
            if not message.reply:
                return
            action = message.subject.rsplit(".", 1)[-1]
            if errors := validate_wire_message(action, message.data, message.subject):
                log.warning("discarded invalid runtime %s", action, extra={"daemon.errors": errors[:3]})
                return
            body = json.loads(message.data)
            try:
                response = await (journal.execute if action == "execute" else journal.inspect)(message.subject, body)
            except Exception:
                log.exception("runtime %s not acknowledged; the sender retries", action)
                return
            await message.respond(encode(response))

        # Separate subscriptions: inspect never queues behind command handling.
        for action in ("execute", "inspect"):
            subject = f"oe.internal.v1.runtime.runtimes.{args.runtime_id}.{action}"
            subscriptions.append(await connection.subscribe(subject, cb=receive))
        await connection.flush()
        worker, outbox = ExecutionWorker(journal), UpdateOutbox(journal, Updates(connection))
        worker.start()
        outbox.start()
        if args.ready_file:
            args.ready_file.write_text("listening")
        log.info("listening", extra={"daemon.runtime_id": args.runtime_id, "daemon.boot_id": journal.boot_id})
        await stopping.wait()
    finally:
        if args.ready_file:
            args.ready_file.unlink(missing_ok=True)
        for subscription in subscriptions:
            await subscription.unsubscribe()
        for task in (worker, outbox):
            if task is not None:
                await task.close()
        if connection is not None:
            await connection.close()
        await store.close()
    return EXIT_OK


def _own(directory: Path) -> int:
    """Exclusive journal ownership for this process's lifetime; released by the kernel if it dies."""
    lock = os.open(directory / LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)
        raise RecoveryRefused(Refusal.OWNED, journal=str(directory)) from None
    return lock


def _migrate(initialization: Initialization, database) -> None:
    if initialization.established and current_revision(database) is None:
        raise RecoveryRefused(Refusal.EMPTY, target=initialization.target, journal=str(database.path))
    journal_store.upgrade(database)


def _unsupervised(unavailable: SupervisionUnavailable) -> int:
    log.error("executor configuration refused: %s", unavailable)
    return EXIT_FAILED


def _refused(refused: RecoveryRefused) -> int:
    log.error("recovery refused: %s %s", refused.reason, json.dumps(refused.evidence, sort_keys=True))
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = parser().parse_args(argv)
    if (not all(TOKEN.match(value) for value in (args.workspace_id, args.runtime_id)) or args.generation < 1
            or args.max_concurrent_attempts < 1):
        log.error("invalid runtime binding")
        return EXIT_FAILED
    if not args.credentials and not args.local_test_account:
        log.error("provision runtime credentials, or explicitly pass --local-test-account for tests")
        return EXIT_FAILED
    lock = None
    try:
        journal_directory, root = locations(args.journal_directory, args.workspace_root)
        configured = executors(args, journal_directory, root)
        lock = _own(journal_directory)
        binding = {"workspace_id": args.workspace_id, "runtime_id": args.runtime_id, "runtime_generation": args.generation}
        database = journal_store.database(journal_directory)
        initialization = Initialization(journal_directory, binding, database.path, initialize=args.initialize)
        _migrate(initialization, database)
        return asyncio.run(serve(args, journal_directory, root, initialization, configured))
    except SupervisionUnavailable as unavailable:
        return _unsupervised(unavailable)
    except RecoveryRefused as refused:
        return _refused(refused)
    except SQLAlchemyError:
        log.exception("runtime journal is unreadable; refusing recovery")
        return EXIT_REFUSED
    except (OSError, nats.errors.Error):
        log.exception("failed")
        return EXIT_FAILED
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
