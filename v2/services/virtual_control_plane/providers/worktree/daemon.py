"""Host supervision of one runtime generation's workspace daemon beside a worktree.

The daemon (`python -m workspace_daemon`) is a separate host process: VCP
starts it detached in its own session, so a VCP restart or shutdown leaves it
running, and reaches it only through the #1118 runtime protocol by runtime ID.
The daemon holds an exclusive `flock` on its journal directory for its whole
life, and the kernel drops that lock when it exits. Liveness and confirmed
termination are read from that lock, never from a PID alone: a free lock is
proof no daemon owns the journal. The recorded PID only selects which process
to signal, after `ps` shows it is the daemon for this journal.

Launching agents and fencing their process trees is #1055's; this module only
runs and stops the daemon.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from openengine.messaging import ErrorCode, ServiceError

from virtual_control_plane.application.ports import Ownership

LOCK_FILE = ".daemon.lock"
"""The daemon's own journal lock (`workspace_daemon.__main__.LOCK_FILE`)."""
RECORD_FILE = "daemon.json"
READY_FILE = "daemon.ready"
LOG_FILE = "daemon.log"
EXIT_REFUSED = 2
START_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.05


def _unavailable(message: str, **details: Any) -> NoReturn:
    raise ServiceError(ErrorCode.UNAVAILABLE, message, retryable=True, details=details or None)


def _terminal(message: str, **details: Any) -> NoReturn:
    raise ServiceError(ErrorCode.PROVIDER_ERROR, message, retryable=False, details=details or None)


class HostDaemon:
    """Starts, rediscovers and stops the daemon for a runtime directory VCP owns.

    `credentials` are the runtime-scoped NATS credentials file; without them
    `local_test_account` must be explicit, as the daemon itself requires.
    """

    def __init__(self, nats_url: str, *, credentials: Path | None = None, local_test_account: bool = False,
                 command: Sequence[str] = (sys.executable, "-m", "workspace_daemon"),
                 start_timeout: float = START_TIMEOUT_SECONDS, stop_timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        if not credentials and not local_test_account:
            raise ValueError("provision runtime credentials or explicitly allow the local test account")  # noqa: TRY003
        self.nats_url, self.credentials, self.local_test_account = nats_url, credentials, local_test_account
        self.command = tuple(command)
        self.start_timeout, self.stop_timeout = start_timeout, stop_timeout
        self._children: dict[str, subprocess.Popen] = {}

    @staticmethod
    def running(journal: Path) -> bool:
        """Whether a process holds the journal lock. Never call while a daemon may be starting."""
        try:
            descriptor = os.open(journal / LOCK_FILE, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(descriptor)  # also releases a lock we just took
        return False

    async def start(self, ownership: Ownership, directory: Path, root: Path, journal: Path, *, initialize: bool) -> None:
        """Make sure this runtime's daemon is listening; idempotent.

        `initialize` permits first setup of an empty journal and is passed only
        before the workspace is ready; an established journal is never recreated.
        """
        self._reap(ownership.runtime_id)
        if self.running(journal):
            return
        ready = directory / READY_FILE
        ready.unlink(missing_ok=True)
        arguments = [*self.command, "--nats-url", self.nats_url, "--journal-directory", str(journal),
                     "--workspace-root", str(root), "--workspace-id", ownership.workspace_id,
                     "--runtime-id", ownership.runtime_id, "--generation", str(ownership.generation),
                     "--ready-file", str(ready)]
        arguments += ["--credentials", str(self.credentials)] if self.credentials else ["--local-test-account"]
        if initialize:
            arguments.append("--initialize")
        log = os.open(directory / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            # Its own session: VCP's signals and exit never reach it, and its group can be signalled as one.
            process = subprocess.Popen(arguments, cwd=directory, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            _terminal(f"cannot start the workspace daemon: {error.strerror or error}")
        finally:
            os.close(log)
        self._children[ownership.runtime_id] = process
        _write(directory / RECORD_FILE, {"pid": process.pid, "workspace_id": ownership.workspace_id,
                                         "runtime_id": ownership.runtime_id, "generation": ownership.generation})
        deadline = time.monotonic() + self.start_timeout
        while not ready.exists():
            if (code := process.poll()) is not None:
                self._children.pop(ownership.runtime_id, None)
                if code == EXIT_REFUSED:
                    _terminal("workspace daemon refused to recover its journal", exit_code=code)
                _unavailable(f"workspace daemon exited with {code} during start-up", exit_code=code)
            if time.monotonic() > deadline:
                await self._signal(process.pid, signal.SIGKILL)
                await asyncio.to_thread(process.wait)
                _unavailable("workspace daemon did not start listening in time")
            await asyncio.sleep(POLL_SECONDS)

    async def stop(self, directory: Path, journal: Path) -> None:
        """Return only once no daemon holds this journal; otherwise raise and keep everything.

        SIGTERM lets the daemon finish its outbox; SIGKILL to its process group
        follows if it does not exit in time.
        """
        runtime_id = directory.name
        self._reap(runtime_id)
        if not self.running(journal):
            return
        pid = self._verified_pid(directory, journal)
        for signum in (signal.SIGTERM, signal.SIGKILL):
            await self._signal(pid, signum)
            deadline = time.monotonic() + self.stop_timeout
            while time.monotonic() < deadline:
                self._reap(runtime_id)
                if not self.running(journal):
                    return
                await asyncio.sleep(POLL_SECONDS)
        _unavailable("workspace daemon termination is not confirmed", runtime_id=runtime_id)

    def _verified_pid(self, directory: Path, journal: Path) -> int:
        """The recorded daemon's PID, only if that process is still this journal's daemon."""
        try:
            record = json.loads((directory / RECORD_FILE).read_text())
            pid = int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            _unavailable("the journal is locked but no daemon record names its process")
        command = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, check=False).stdout
        if f"--journal-directory {journal} " not in command + " ":
            _unavailable("the journal is locked by a process this record does not identify", pid=pid)
        return pid

    @staticmethod
    async def _signal(pid: int, signum: int) -> None:
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass

    def _reap(self, runtime_id: str) -> None:
        if (child := self._children.get(runtime_id)) is not None and child.poll() is not None:
            del self._children[runtime_id]


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
