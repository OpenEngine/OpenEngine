"""Materialize repository checkouts before offering WorkOrders."""

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock

from engine.runtime import EngineConfigError


def _clone_failure(error: Exception) -> str:
    """What went wrong, in git's own words, with a pointer where the cause is known."""
    if isinstance(error, subprocess.TimeoutExpired):
        return f"git clone timed out after {error.timeout:g} seconds."
    if isinstance(error, subprocess.CalledProcessError):
        said = " ".join(line.strip() for line in (error.stderr or "").splitlines() if line.strip())
        said = said[-500:] if said else f"git exited with status {error.returncode}."
        if "Permission denied (publickey)" in said:
            said += (
                " The daemon has no SSH agent and uses the host's default key; to use a specific"
                " account's key, set [github] ssh_key in the engine configuration and restart."
            )
        return said if said.endswith(".") else f"{said}."
    return f"{type(error).__name__}: {error}."


def ensure_repository_checkouts(repos: Mapping[str, str], *, clone_urls: Mapping[str, str] | None = None) -> None:
    """Clone missing checkouts from explicit URLs or the GitHub SSH default.

    Local aliases retain their existing meaning. Existing paths are never
    updated; a temporary sibling keeps failed clones out of the configured path.
    Paths follow the WorkOrder API: expand ~, then resolve against the cwd.
    """
    for name, configured_path in repos.items():
        path = Path(configured_path).expanduser().absolute()
        if path.exists() or path.is_symlink():
            continue
        if not (clone_urls and name in clone_urls) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the lock file: unlinking it can split waiters across inodes.
            with FileLock(path.parent / f".engine-clone-{path.name}.lock"):
                if path.exists() or path.is_symlink():
                    continue
                logging.getLogger(__name__).info("Cloning %s into %s", name, path)
                with TemporaryDirectory(prefix=".engine-clone-", dir=path.parent) as staging:
                    checkout = Path(staging) / "checkout"
                    subprocess.run(
                        ["git", "clone", "--", (clone_urls or {}).get(name, f"git@github.com:{name}.git"), str(checkout)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                    checkout.rename(path)
        except (OSError, subprocess.SubprocessError) as error:
            raise EngineConfigError(
                f"Could not clone [repos] {name} into {path}: {_clone_failure(error)} "
                "Check the host's remote access, network and destination permissions, then retry."
            ) from error
