"""The repository a command is run from, as a backend should be told about it.

A local daemon can check out the very directory you are in, so it is sent the
path. A remote one cannot, so it is sent the project -- `owner/repo` from
`origin` -- which it resolves against its own `[repos]`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_REMOTE = re.compile(r"(?:[:/])([^/:]+/[^/]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Repository:
    root: Path
    project: str
    """`owner/repo` from origin, or empty when there is no origin."""
    branch: str
    """The checked-out branch, or empty when HEAD is detached."""
    dirty: bool

    def target(self, *, local: bool) -> str:
        return str(self.root) if local or not self.project else self.project


def current(directory: Path | None = None) -> Repository | None:
    """The git repository containing `directory`, or None outside one."""
    where = str(directory or Path.cwd())
    root = _git(where, "rev-parse", "--show-toplevel")
    if not root:
        return None
    remote = _git(root, "remote", "get-url", "origin")
    match = _REMOTE.search(remote) if remote else None
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    status = _git(root, "status", "--porcelain")
    return Repository(Path(root), match.group(1) if match else "", branch, bool(status))


def _git(directory: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", directory, *arguments], capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


__all__ = ["Repository", "current"]
