"""Materialize repository checkouts before offering WorkOrders."""

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, unquote, urlsplit

from filelock import FileLock

from engine.runtime import EngineConfigError


def redact_clone_message(message: str, remote: str = "") -> str:
    """Remove URLs and remote credentials even when git echoes them separately."""
    try:
        parsed = urlsplit(remote)
        secrets = [parsed.username, parsed.password, *(value for _, value in parse_qsl(parsed.query))]
    except ValueError:
        secrets = []
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        message = message.replace(secret, "[redacted]").replace(unquote(secret), "[redacted]")
    # Replace credentials first so URL delimiters cannot leave a secret suffix.
    # Drop the entire URL: credentials can appear in userinfo, query or path.
    message = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", "[redacted URL]", message)
    return message


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
        remote = (clone_urls or {}).get(name, f"git@github.com:{name}.git")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the lock file: unlinking it can split waiters across inodes.
            with FileLock(path.parent / f".engine-clone-{path.name}.lock"):
                if path.exists() or path.is_symlink():
                    continue
                logging.getLogger(__name__).info("Cloning %s into %s", redact_clone_message(name, remote), redact_clone_message(str(path), remote))
                with TemporaryDirectory(prefix=".engine-clone-", dir=path.parent) as staging:
                    checkout = Path(staging) / "checkout"
                    subprocess.run(
                        ["git", "clone", "--", remote, str(checkout)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                    checkout.rename(path)
        except (OSError, subprocess.SubprocessError) as error:
            if isinstance(error, subprocess.TimeoutExpired):
                reason = "git clone timed out after 300 seconds"
            elif isinstance(error, subprocess.CalledProcessError):
                stderr = error.stderr or ""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                reason = stderr.strip() or f"git clone exited with status {error.returncode}"
            else:
                reason = str(error)
            hint = " Check [github] ssh_key for the daemon's SSH identity." if "publickey" in reason.lower() else ""
            message = (
                f"Could not clone [repos] {name} into {path}; check the host's "
                "remote access, network and destination permissions, then retry. "
                f"{reason}{hint}"
            )
            # Chained subprocess exceptions contain the unredacted command URL.
            raise EngineConfigError(redact_clone_message(message, remote)) from None
