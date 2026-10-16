"""Host-held VM leases survive crashes as unlocked files, never stale PID guesses."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

MANAGED = "openengine.sandbox"
SCOPE = "openengine.scope"
LEASE = "openengine.lease"
_ID = re.compile(r"[0-9a-f]{32}")


class Ownership:
    def __init__(self, directory: str | Path | None):
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(tempfile.gettempdir())
            / f"openengine-sandboxes-{getattr(os, 'getuid', lambda: 0)()}"
        ).resolve() / "sandbox-leases"
        self.scope = hashlib.sha256(os.fsencode(self.directory)).hexdigest()

    @contextmanager
    def lock(self, identity: str, *, blocking: bool = False):
        import fcntl

        if not _ID.fullmatch(identity):
            message = "invalid sandbox lease identity"
            raise ValueError(message)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (identity + ".lease")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
            yield path if acquired else None
        finally:
            os.close(fd)

    @contextmanager
    def create(self):
        identity = uuid.uuid4().hex
        with self.lock(identity) as path:
            assert path is not None
            try:
                yield {MANAGED: "1", SCOPE: self.scope, LEASE: identity}
            finally:
                path.unlink(missing_ok=True)

    def orphan(self, machine: dict) -> str | None:
        labels = machine.get("labels")
        if (
            not isinstance(labels, dict)
            or labels.get(MANAGED) != "1"
            or labels.get(SCOPE) != self.scope
        ):
            return None
        identity, name = labels.get(LEASE), machine.get("name")
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or name != f"oe-sandbox-{identity}"
        ):
            message = "invalid managed sandbox ownership labels"
            raise ValueError(message)
        return identity

    def prune(self) -> None:
        """Remove unlocked bookkeeping left by a crash before VM registration."""
        for path in self.directory.glob("*.lease"):
            if _ID.fullmatch(path.stem):
                with self.lock(path.stem) as lease:
                    if lease is not None:
                        lease.unlink(missing_ok=True)
