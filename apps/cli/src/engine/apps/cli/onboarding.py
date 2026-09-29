"""`engine init`: offer the repository you are standing in for WorkOrders.

Onboarding is one `[repos]` entry in the service's `engine.toml` -- the table
the web interface's repository dropdown is built from -- followed by a restart,
because the service reads that file once, at startup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from engine.apps.cli import daemon

EXIT_OK = 0
EXIT_FAILED = 1

_TABLE_HEADER = re.compile(r"^\s*\[\s*repos\s*\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser("init", help="offer this repository for WorkOrders")
    command.add_argument("--name", help="how the repository is listed (default: its origin's owner/name)")
    command.add_argument("--config", metavar="PATH", help="the engine.toml to add it to (default: the service's)")
    command.add_argument("--no-restart", action="store_true", help="do not restart a running service")


def repository_root(directory: Path) -> Path:
    try:
        output = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{directory} is not inside a git repository") from error
    return Path(output.strip()).resolve()


def default_name(root: Path) -> str:
    """`owner/name` from the `origin` remote, else the checkout's directory name."""
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return root.name
    # Both `https://host/owner/name.git` and `git@host:owner/name.git`.
    parts = [part for part in re.split(r"[/:]", remote.removesuffix("/").removesuffix(".git")) if part]
    return "/".join(parts[-2:]) if len(parts) >= 3 else root.name


def target_config(explicit: str | None) -> Path:
    """The file the running service reads: its recorded one, else the installer's."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    record = daemon.read_record()
    return Path(record.spec.config) if record is not None else daemon.config_path()


def add_repository(text: str, name: str, root: Path) -> str:
    """`text` with `name = root` added to its `[repos]` table, comments intact."""
    entry = f"{json.dumps(name)} = {json.dumps(str(root))}"
    lines = text.splitlines()
    header = next((index for index, line in enumerate(lines) if _TABLE_HEADER.match(line)), None)
    if header is None:
        if "repos" in tomllib.loads(text):
            raise RuntimeError("repos is not written as a [repos] table; add the entry by hand")
        separator = "" if not text.strip() else "\n" if text.endswith("\n") else "\n\n"
        return f"{text}{separator}[repos]\n{entry}\n"
    end = next(
        (index for index in range(header + 1, len(lines)) if _ANY_HEADER.match(lines[index])),
        len(lines),
    )
    # After the table's last entry, so blank lines and comments that introduce
    # the next table stay with it.
    last = next(
        (index for index in range(end - 1, header, -1)
         if lines[index].strip() and not lines[index].lstrip().startswith("#")),
        header,
    )
    lines.insert(last + 1, entry)
    return "\n".join(lines) + "\n"


def restart_service() -> str:
    """Restart a running service so it reads the new entry; say what happened."""
    try:
        _backend, spec = daemon.current()
        state, _body = daemon.health(spec.url)
        if state not in {"ready", "starting"}:
            return "It will be offered when OpenEngine next starts (engine daemon start)."
        daemon.stop_service()
        daemon.start_service()
    except (OSError, RuntimeError, ValueError) as error:
        return f"Could not restart OpenEngine ({error}); restart it to offer the repository."
    return f"Restarted OpenEngine at {spec.url}; the repository is in its dropdown."


def main(arguments: argparse.Namespace) -> int:
    try:
        root = repository_root(Path.cwd())
        name = arguments.name or default_name(root)
        path = target_config(arguments.config)
        if not path.is_file():
            raise RuntimeError(f"no configuration at {path}; run the installer, or pass --config")
        text = path.read_text(encoding="utf-8")
        try:
            repos = tomllib.loads(text).get("repos", {})
        except tomllib.TOMLDecodeError as error:
            raise RuntimeError(f"invalid TOML in {path}: {error}") from error
        existing = next(
            (key for key, value in repos.items()
             if isinstance(value, str) and _resolved(value, path.parent) == root),
            None,
        )
        if existing is not None:
            print(f"{root} is already onboarded as {existing!r} in {path}")
            return EXIT_OK
        if name in repos:
            raise RuntimeError(f"{name!r} already names {repos[name]} in {path}; choose another with --name")
        updated = add_repository(text, name, root)
        if tomllib.loads(updated).get("repos", {}).get(name) != str(root):
            raise RuntimeError(f"could not add {name!r} to {path}; add it under [repos] by hand")
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"engine init: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Onboarded {root} as {name!r} in {path}")
    if not arguments.no_restart:
        print(restart_service())
    return EXIT_OK


def _resolved(value: str, base: Path) -> Path:
    """A `[repos]` path as the service resolves it: `~` expanded, relative to the config."""
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()
