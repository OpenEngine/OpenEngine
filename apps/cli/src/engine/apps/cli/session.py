"""`engine agent claude`: Claude Code as the implementation node of a run.

The daemon starts a run of its built-in session graph: it checks the
repository out into a workspace of its own, then binds the run's repository
tools -- `git_subcommand`, `open_pull_request`, whatever `[sessions] tools` in
engine.toml grants -- to that checkout, exactly as it does for an
implementation node. Instead of opening an ACP session, the node hands those
tools to this command, which runs `claude` in the checkout, in this terminal.
When claude exits, the node finishes and the run with it.

The tools are served from the daemon's own process on 127.0.0.1 and the
checkout is on the daemon's disk, so the backend has to be this machine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from engine.apps.cli import connect
from engine.cli import backends, repository
from engine.cli.backends import BackendError
from engine.cli.http import Client, RequestFailed

EXIT_OK = 0
EXIT_FAILED = 1
POLL_SECONDS = 0.5


def add_parser(commands: argparse._SubParsersAction) -> None:
    agent = commands.add_parser("agent", help="drive an implementation node from this terminal")
    harnesses = agent.add_subparsers(dest="harness", required=True)
    claude = harnesses.add_parser(
        "claude",
        help="Claude Code in a fresh workspace, with the run's git and pull request tools",
        description=(
            "Start a run whose implementation node is Claude Code in this terminal, working in a "
            "fresh workspace with the tools [sessions] grants in engine.toml. Arguments after -- go to claude."
        ),
    )
    claude.add_argument("--repo", default=".", help="a path inside the repository (default: the current directory)")
    claude.add_argument("--base", default="", metavar="REF", help="what the workspace starts from (default: the backend's)")
    claude.add_argument("--backend", metavar="NAME", help="the backend to use; it must run on this machine")
    claude.add_argument("claude_args", nargs=argparse.REMAINDER, help="passed to claude after --")


def main(arguments: argparse.Namespace) -> int:
    if shutil.which("claude") is None:
        print("engine: claude is not installed; see https://docs.claude.com/en/docs/claude-code", file=sys.stderr)
        return EXIT_FAILED
    try:
        backend = backends.load().selected(arguments.backend)
        if not backend.is_local:
            raise RuntimeError(
                f"backend {backend.name} is not on this machine; a session's workspace and tools "
                "live with its daemon, so run engine agent claude there"
            )
        connect.ensure_ready(backend)
        client = Client(backend)
        session = start(client, arguments)
    except (BackendError, RequestFailed, RuntimeError, ValueError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_FAILED
    workspace = session["workspace"]
    print(f"Run {session['runId']}: workspace {workspace['path']} on {workspace['ref']}", file=sys.stderr)
    status = EXIT_FAILED
    try:
        with tempfile.TemporaryDirectory(prefix="engine-session-") as directory:
            status = run_claude(workspace["path"], claude_command(session, Path(directory), arguments.claude_args))
    finally:
        try:
            client.post(f"/sessions/{quote(session['sessionId'])}/end", {})
        except RequestFailed as error:
            print(f"engine: could not end the session: {error}", file=sys.stderr)
    print(
        f"Session ended. The work is on {workspace['ref']} at {workspace['path']}; "
        f"see `engine run get {session['runId']} --pretty`.",
        file=sys.stderr,
    )
    return status


def start(client: Client, arguments: argparse.Namespace) -> dict[str, Any]:
    """Start the run, then wait for its node to be ready for a terminal."""
    found = repository.current(Path(arguments.repo))
    if found is None:
        raise RuntimeError(f"{arguments.repo} is not inside a git repository")
    session = client.post("/sessions", {"agent": "claude", "repository": found.target(local=True), "baseRef": arguments.base})
    told = False
    while session["status"] == "starting":
        if not told:
            print(f"Starting run {session['runId']}…", file=sys.stderr)
            told = True
        time.sleep(POLL_SECONDS)
        session = client.get(f"/sessions/{quote(session['sessionId'])}")
    if session["status"] != "ready":
        raise RuntimeError(f"run {session['runId']} did not start a session: {session.get('error') or session['status']}")
    return session


def claude_command(session: dict[str, Any], directory: Path, passed: list[str]) -> list[str]:
    """The claude invocation: the run's MCP server, its instructions and Engine's shell rules."""
    mcp = session["mcp"]
    servers = {"mcpServers": {mcp["name"]: {"command": mcp["command"], "args": list(mcp["args"])}}}
    (directory / "mcp.json").write_text(json.dumps(servers), encoding="utf-8")
    command = ["claude"]
    if session.get("settings"):
        (directory / "settings.json").write_text(json.dumps(session["settings"]), encoding="utf-8")
        command += ["--settings", str(directory / "settings.json")]
    # `--mcp-config` takes several files, and would take a prompt passed after
    # it for one more; an option following it is what ends the list.
    command += [
        "--mcp-config", str(directory / "mcp.json"),
        "--append-system-prompt", session.get("instructions") or "You are working in an OpenEngine workspace.",
    ]
    return [*command, *(passed[1:] if passed[:1] == ["--"] else passed)]


def run_claude(root: str, command: list[str]) -> int:
    """Hand this terminal to claude until it exits.

    Ctrl-C belongs to claude, which uses it to interrupt a turn, so this
    process ignores it rather than dying and leaving the run's node waiting.
    """
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(command, cwd=root)
    finally:
        signal.signal(signal.SIGINT, previous)
