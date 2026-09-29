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
from engine.runtime.change_requests import remote_project

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


def origin_project(root: Path) -> str | None:
    """The `origin` remote's project, as the web interface names it."""
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return remote_project(remote)


def login_warning(document: dict, config: Path, project: str | None) -> str | None:
    """Why onboarding `project` lets more people sign in, when GitHub login is on.

    The service admits anyone who can write to a GitHub repository under
    `[repos]`, so a new one widens sign-in to its collaborators. Mirrors the
    web interface's checks: a login setting in the file, its environment
    override, or the client secret in the `.env` beside it.
    """
    if project is None:
        return None
    host, _, rest = project.partition("/")
    aliases = document.get("github", {}).get("host_aliases", {})
    if "/" in rest and host.lower() not in {alias.lower() for alias in aliases}:
        return None  # Not on GitHub, so GitHub cannot vouch for its writers.
    if not (
        document.get("github_login_client_id")
        or document.get("github_login_redirect_uri")
        or any(os.environ.get(f"ENGINE_GITHUB_LOGIN_{key}") for key in ("CLIENT_ID", "REDIRECT_URI", "CLIENT_SECRET"))
        or _has_login_secret(config.parent / ".env")
    ):
        return None
    return (
        f"Warning: GitHub sign-in is enabled, so anyone with write access to {project} "
        "can now sign in to OpenEngine."
    )


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
        project = origin_project(root)
        name = arguments.name or project or root.name
        path = target_config(arguments.config)
        if not path.is_file():
            raise RuntimeError(f"no configuration at {path}; run the installer, or pass --config")
        text = path.read_text(encoding="utf-8")
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise RuntimeError(f"invalid TOML in {path}: {error}") from error
        repos = document.get("repos", {})
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
        # Created owner-only and given the original's mode, so replacing the
        # file never loosens the installer's 0600.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), path.stat().st_mode & 0o777)
            handle.write(updated)
        os.replace(temporary, path)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"engine init: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Onboarded {root} as {name!r} in {path}")
    if (warning := login_warning(document, path, project)) is not None:
        print(warning, file=sys.stderr)
    if not arguments.no_restart:
        print(restart_service())
    return EXIT_OK


def _resolved(value: str, base: Path) -> Path:
    """A `[repos]` path as the service resolves it: `~` expanded, relative to the config."""
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def _has_login_secret(env_file: Path) -> bool:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        key, separator, value = line.removeprefix("export ").partition("=")
        if separator and key.strip() == "ENGINE_GITHUB_LOGIN_CLIENT_SECRET":
            return bool(value.strip().strip("'\""))
    return False
