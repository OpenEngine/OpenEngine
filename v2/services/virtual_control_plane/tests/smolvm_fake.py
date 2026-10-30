"""A deterministic stand-in for the `smolvm` CLI, for CI without virtualization.

`install(root)` writes an executable `smolvm` shim that runs this module with
`root` as its state directory. Machines are JSON records under `root/machines`;
each has a guest filesystem under `root/guests/<name>`, and `machine exec` runs
the command on the host with guest paths (`/workspace`, `/oe-vcp`) rewritten
into it -- so real `sh`, `git` and `tar` do the work the adapter asks for.
Every invocation is appended to `root/commands.jsonl`.

Faults: `root/faults.json` maps a subcommand (`create`, `start`, `exec`, `cp`,
`ls`, `status`) to how many of its next invocations fail with exit 1; a test
writes it to inject transient failures.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

GUEST_PREFIXES = ("/workspace", "/oe-vcp")


def install(root: Path) -> Path:
    """The path of an executable `smolvm` shim backed by `root`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "machines").mkdir(exist_ok=True)
    (root / "guests").mkdir(exist_ok=True)
    binary = root / "bin" / "smolvm"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(f'#!/bin/sh\nexec {sys.executable} -I {__file__} {root} "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def commands(root: Path) -> list[list[str]]:
    log = root / "commands.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def machines(root: Path) -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text()) for path in sorted((root / "machines").glob("*.json"))}


def save_machine(root: Path, record: dict) -> None:
    (root / "machines" / f"{record['name']}.json").write_text(json.dumps(record))
    (root / "guests" / record["name"]).mkdir(parents=True, exist_ok=True)


def inject_faults(root: Path, **counts: int) -> None:
    (root / "faults.json").write_text(json.dumps(counts))


def guest(root: Path, name: str) -> Path:
    return root / "guests" / name


def _rewrite(root: Path, name: str, argument: str) -> str:
    for prefix in GUEST_PREFIXES:
        if argument == prefix or argument.startswith(prefix + "/"):
            return str(guest(root, name)) + argument
    return argument


def _option(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _fault(root: Path, action: str) -> bool:
    faults = root / "faults.json"
    counts = json.loads(faults.read_text()) if faults.exists() else {}
    if counts.get(action, 0) <= 0:
        return False
    counts[action] -= 1
    faults.write_text(json.dumps(counts))
    return True


def _load(root: Path, name: str) -> dict:
    path = root / "machines" / f"{name}.json"
    if not path.exists():
        print(f"Error: machine '{name}' not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def main(root: Path, arguments: list[str]) -> int:  # noqa: PLR0911, PLR0912 -- one branch per CLI verb
    with (root / "commands.jsonl").open("a") as log:
        log.write(json.dumps(arguments) + "\n")
    if arguments[:1] != ["machine"] or len(arguments) < 2:
        print("unsupported invocation", file=sys.stderr)
        return 2
    action, args = arguments[1], arguments[2:]
    if _fault(root, action):
        print(f"injected {action} failure", file=sys.stderr)
        return 1
    if action == "ls":
        print(json.dumps(list(machines(root).values())))
        return 0
    if action == "create":
        name = _option(args, "--name")
        if (root / "machines" / f"{name}.json").exists():
            print(f"Error: machine '{name}' already exists", file=sys.stderr)
            return 1
        labels = dict(args[index + 1].split("=", 1) for index, flag in enumerate(args) if flag == "--label")
        command = args[args.index("--") + 1:] if "--" in args else []
        record = {"name": name, "labels": labels, "image": _option(args, "--image"), "state": "created",
                  "cpus": int(_option(args, "--cpus")) if "--cpus" in args else 4,
                  "memory_mib": int(_option(args, "--mem")) if "--mem" in args else 8192,
                  "storage_gb": int(_option(args, "--storage")) if "--storage" in args else None,
                  "network": "--net" in args, "cmd": command}
        save_machine(root, record)
        return 0
    if action == "status":
        print(json.dumps(_load(root, _option(args, "--name"))))
        return 0
    if action == "start":
        record = _load(root, _option(args, "--name"))
        record["state"] = "running"
        save_machine(root, record)
        return 0
    if action == "delete":
        name = _option(args, "--name")
        _load(root, name)
        (root / "machines" / f"{name}.json").unlink()
        shutil.rmtree(guest(root, name), ignore_errors=True)
        return 0
    if action == "cp":
        source, destination = args[-2:]
        if ":" in destination:
            name, path = destination.split(":", 1)
            _load(root, name)
            target = Path(_rewrite(root, name, path))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return 0
        print("only host-to-machine copies are faked", file=sys.stderr)
        return 1
    if action == "exec":
        name = _option(args, "--name")
        record = _load(root, name)
        if record["state"] != "running":
            print(f"Error: machine '{name}' is not running", file=sys.stderr)
            return 1
        command = [_rewrite(root, name, argument) for argument in args[args.index("--") + 1:]]
        guest(root, name).mkdir(parents=True, exist_ok=True)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(guest(root, name))}
        return subprocess.run(command, cwd=guest(root, name), env=environment, check=False).returncode
    print(f"unsupported machine subcommand {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2:]))
