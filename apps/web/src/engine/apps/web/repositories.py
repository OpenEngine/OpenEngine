"""Materialize configured GitHub checkouts before offering WorkOrders."""

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock

from engine.runtime import EngineConfigError


def ensure_repository_checkouts(repos: Mapping[str, str]) -> None:
    """Clone missing owner/repo entries using the host's existing SSH identity.

    Local aliases retain their existing meaning. Existing paths are never
    updated; a temporary sibling keeps failed clones out of the configured path.
    Paths follow the WorkOrder API: expand ~, then resolve against the cwd.
    """
    for name, configured_path in repos.items():
        path = Path(configured_path).expanduser().absolute()
        if path.exists() or path.is_symlink():
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
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
                        ["git", "clone", "--", f"git@github.com:{name}.git", str(checkout)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                    checkout.rename(path)
        except (OSError, subprocess.SubprocessError) as error:
            raise EngineConfigError(
                f"Could not clone [repos] {name} into {path}; check the host's "
                "GitHub SSH access, network and destination permissions, then retry. "
                f"({type(error).__name__})"
            ) from error
