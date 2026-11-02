"""Owned-file writes for the worktree adapter: durable, private, and never through a symlink."""

from __future__ import annotations

import os
from pathlib import Path


def write_atomically(path: Path, content: str) -> None:
    """Replace `path` with `content` (0600) after it is on disk; a symlink at the temporary name is refused."""
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
