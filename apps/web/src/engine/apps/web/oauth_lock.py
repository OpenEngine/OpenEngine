"""Cross-process locking for shared OAuth credential entries."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from platformdirs import user_state_path


def credential_lock_path(service: str, username: str) -> Path:
    """Return one non-secret, per-user lock path for a keychain identity."""
    identity = hashlib.sha256(f"{service}\0{username}".encode()).hexdigest()
    directory = user_state_path("openengine") / "oauth-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"{identity}.lock"


def _credential_file_lock(service: str, username: str) -> FileLock:
    """Create the lock outside the event-loop thread, including mkdir()."""
    return FileLock(credential_lock_path(service, username))


@asynccontextmanager
async def credential_lock(
    service: str, username: str, *, timeout_seconds: float = 10
) -> AsyncIterator[bool]:
    """Acquire an OS-backed lock without blocking the asyncio event loop.

    A timeout deliberately fails closed: callers leave credentials untouched and
    let their current request fail rather than racing a single-use refresh token.

    ``flock`` is reliable for this local per-user state directory. It is not a
    distributed lock and must not be moved to an NFS/shared filesystem.
    """
    try:
        lock = await asyncio.to_thread(_credential_file_lock, service, username)
        await asyncio.to_thread(lock.acquire, timeout=timeout_seconds)
    except (Timeout, OSError):
        yield False
        return
    try:
        yield True
    finally:
        release = asyncio.create_task(asyncio.to_thread(lock.release))
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            # Shield lets the OS lock release complete even while application
            # shutdown cancels this request.
            await asyncio.shield(release)
            raise
