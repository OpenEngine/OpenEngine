"""`engine init`: offer the repository you are standing in for WorkOrders.

Onboarding is one `[repos]` entry in the service's `engine.toml` -- the table
the web interface's repository dropdown is built from -- followed by a restart,
because the service reads that file once, at startup.

It also asks how WorkOrders on the repository reach its forge. Both connected
choices only need a GitHub login, so they are answered with the steps to get
one; disconnected is recorded under `[repo_modes]`, which makes every
WorkOrder on the repository run disconnected.

Last it asks how WorkOrders' requests are approved: automatically everywhere
(`approvals.auto_approve`), automatically only on trusted repositories (named
under `[trusted_repos]`), or by a person every time.
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
from engine.domain import ForgeMode
from engine.runtime.change_requests import remote_project

EXIT_OK = 0
EXIT_FAILED = 1

_ANY_HEADER = re.compile(r"^\s*\[")

OAUTH = "oauth"
CLI = "cli"
DISCONNECTED = str(ForgeMode.DISCONNECTED)
#: The choices `engine init` offers, in the order it lists them.
MODES = {
    OAUTH: "Git OAuth (connected, recommended)",
    CLI: "Git CLI (connected)",
    DISCONNECTED: "Disconnected",
}
CONNECTED_EXPLAINED = (
    "Connected WorkOrders push their branch, open a pull request and add "
    "comments to it automatically; disconnected ones work only in their own checkout."
)
NEXT_STEPS = {
    OAUTH: (
        "Next: run `engine connect github --open` and approve OpenEngine on GitHub "
        "(or choose GitHub OAuth under Settings > GitHub in the web interface)."
    ),
    CLI: (
        "Next: install the GitHub CLI (https://cli.github.com), run `gh auth login`, "
        "then `engine connect gh` (or choose GH CLI under Settings > GitHub)."
    ),
    DISCONNECTED: (
        "WorkOrders on this repository will run disconnected: nothing is pushed, "
        "no pull request is opened and no comment is posted."
    ),
}


AUTO = "auto"
TRUSTED = "trusted"
MANUAL = "manual"
#: The approval modes `engine init` offers, in the order it lists them.
APPROVALS = {
    AUTO: "Auto-approve (every repository)",
    TRUSTED: "Trusted repos (auto-approve only repositories marked trusted, like this one)",
    MANUAL: "Manual (ask before each change)",
}
APPROVALS_EXPLAINED = (
    "Agents ask before they edit files or run commands; auto-approved WorkOrders "
    "answer yes for you. Auto-approve and manual apply to every repository."
)


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser("init", help="offer this repository for WorkOrders")
    command.add_argument("--name", help="how the repository is listed (default: its origin's owner/name)")
    command.add_argument("--config", metavar="PATH", help="the engine.toml to add it to (default: the service's)")
    command.add_argument("--no-restart", action="store_true", help="do not restart a running service")
    command.add_argument(
        "--mode", choices=tuple(MODES),
        help="how its WorkOrders reach the forge (default: ask, or oauth without a terminal)",
    )
    command.add_argument(
        "--approval", choices=tuple(APPROVALS),
        help="how its WorkOrders' requests are approved (default: ask, or unchanged without a terminal)",
    )


def choose_mode(explicit: str | None) -> str:
    """The mode given, else the one asked for at a terminal, else OAuth."""
    if explicit:
        return explicit
    if not sys.stdin.isatty():
        return OAUTH
    return prompt_choice(
        "How should WorkOrders on this repository reach GitHub?", CONNECTED_EXPLAINED, "Mode", MODES, OAUTH
    )


def choose_approval(explicit: str | None, current: str) -> str | None:
    """The approval mode given, else the one asked for at a terminal, else `None`.

    `current`, the mode the configuration is already in, is the default.
    """
    if explicit:
        return explicit
    if not sys.stdin.isatty():
        return None
    return prompt_choice(
        "How should WorkOrders' requests be approved?", APPROVALS_EXPLAINED, "Approval", APPROVALS, current
    )


def prompt_choice(question: str, explained: str, label: str, choices: dict[str, str], default: str) -> str:
    """One of `choices` picked by number or name at the terminal, `default` on Enter or EOF."""
    keys = list(choices)
    print(question)
    print(explained)
    for number, key in enumerate(keys, 1):
        print(f"  {number}) {choices[key]}")
    while True:
        try:
            answer = input(f"{label} [{keys.index(default) + 1}]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        if answer in choices:
            return answer
        print(f"Choose 1-{len(keys)}.")


def current_approval(document: dict, name: str) -> str:
    """The approval mode `document` is in, as repository `name` sees it.

    A repository not yet named under `[trusted_repos]` is manual, however many
    others are trusted: trust is given one repository at a time.
    """
    if document.get("approvals", {}).get("auto_approve") is True:
        return AUTO
    trusted = document.get("trusted_repos", {})
    return TRUSTED if trusted.get(name) is True else MANUAL


def set_approval(text: str, document: dict, name: str, approval: str) -> str:
    """`text` switched to `approval`, touching only the settings that change."""
    if document.get("approvals", {}).get("auto_approve", False) != (approval == AUTO):
        text = set_entry(text, "approvals", "auto_approve", approval == AUTO)
    trusted = document.get("trusted_repos", {}).get(name, False)
    if approval == TRUSTED and trusted is not True:
        text = set_entry(text, "trusted_repos", name, True)
    elif approval == MANUAL and trusted is True:
        text = set_entry(text, "trusted_repos", name, False)
    return text


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
    return add_entry(text, "repos", name, str(root))


def set_entry(text: str, table: str, key: str, value: str | bool) -> str:
    """`text` with `[table]`'s `key` set to `value`, replacing the line that sets it."""
    lines = text.splitlines()
    table_header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    header = next((index for index, line in enumerate(lines) if table_header.match(line)), None)
    if header is not None:
        setting = re.compile(rf"^\s*({re.escape(key)}|{re.escape(json.dumps(key))})\s*=")
        for index in range(header + 1, len(lines)):
            if _ANY_HEADER.match(lines[index]):
                break
            if setting.match(lines[index]):
                lines[index] = _entry(key, value)
                return "\n".join(lines) + "\n"
    return add_entry(text, table, key, value)


def add_entry(text: str, table: str, key: str, value: str | bool) -> str:
    """`text` with `key = value` added to its `[table]` table, comments intact."""
    entry = _entry(key, value)
    lines = text.splitlines()
    table_header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    header = next((index for index, line in enumerate(lines) if table_header.match(line)), None)
    if header is None:
        if table in tomllib.loads(text):
            raise RuntimeError(f"{table} is not written as a [{table}] table; add the entry by hand")
        separator = "" if not text.strip() else "\n" if text.endswith("\n") else "\n\n"
        return f"{text}{separator}[{table}]\n{entry}\n"
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
        if existing is None and name in repos:
            raise RuntimeError(f"{name!r} already names {repos[name]} in {path}; choose another with --name")
        mode = choose_mode(arguments.mode)
        updated = text
        if existing is None:
            updated = add_repository(updated, name, root)
        else:
            name = existing
        recorded = document.get("repo_modes", {}).get(name)
        if mode == DISCONNECTED and recorded != DISCONNECTED:
            if recorded is not None:
                raise RuntimeError(f"repo_modes.{name} is {recorded!r} in {path}; set it to \"disconnected\" by hand")
            updated = add_entry(updated, "repo_modes", name, DISCONNECTED)
        elif mode != DISCONNECTED and recorded == DISCONNECTED:
            raise RuntimeError(
                f"{name!r} is disconnected under [repo_modes] in {path}; remove that entry to connect it"
            )
        approval = choose_approval(arguments.approval, current_approval(document, name))
        if approval is not None:
            updated = set_approval(updated, document, name, approval)
        if updated == text:
            print(f"{root} is already onboarded as {existing!r} in {path}")
            print(NEXT_STEPS[mode])
            return EXIT_OK
        written = tomllib.loads(updated)
        if (
            written.get("repos", {}).get(name) != repos.get(name, str(root))
            or (mode == DISCONNECTED and written.get("repo_modes", {}).get(name) != DISCONNECTED)
            or (approval is not None and current_approval(written, name) != approval)
        ):
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
    if existing is None:
        print(f"Onboarded {root} as {name!r} in {path}")
        if (warning := login_warning(document, path, project)) is not None:
            print(warning, file=sys.stderr)
    else:
        print(f"Updated {name!r} in {path}")
    if not arguments.no_restart:
        print(restart_service())
    print(NEXT_STEPS[mode])
    return EXIT_OK


def _entry(key: str, value: str | bool) -> str:
    """One TOML line, quoting the key only where TOML needs it."""
    written = key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)
    return f"{written} = {json.dumps(value)}"


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
