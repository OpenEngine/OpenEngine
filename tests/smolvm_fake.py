"""Subprocess-level SmolVM CLI fake. ROOT is supplied by the test launcher."""

import json
import os
from pathlib import Path
import shutil
import sys

root = Path(ROOT)
arguments = sys.argv[1:]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps(arguments) + "\n")
if arguments == ["--version"]:
    print("smolvm 1.25.2")
    sys.exit(0)

action, args = arguments[1], arguments[2:]
if (root / f"fail-{action}").exists():
    print(f"injected {action} failure", file=sys.stderr)
    sys.exit(1)
def option(name):
    return args[args.index(name) + 1]

def guest(path, machine):
    directory = root / machine
    path = Path(path)
    if path.is_relative_to(directory):
        return path
    return directory / str(path).lstrip("/")

if action == "create":
    (root / option("--name")).mkdir()
elif action == "start":
    assert (root / option("--name")).exists()
elif action == "delete":
    shutil.rmtree(root / option("--name"))
elif action == "ls":
    print(json.dumps([{"name": path.name} for path in root.glob("oe-sandbox-*")]))
elif action == "cp":
    source, destination = args
    if ":" in source:
        machine, path = source.split(":", 1)
        source = guest(path, machine)
    else:
        machine, path = destination.split(":", 1)
        destination = guest(path, machine)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if destination.name == "oe-sandbox-transfer.py":
        # Only substitute the VM's absolute workspace for the fake filesystem.
        destination.write_text(destination.read_text().replace(
            'WORKSPACE = Path("/workspace")', f'WORKSPACE = Path({str(root / machine / "workspace")!r})',
        ))
elif action == "exec":
    machine = option("--name")
    command = args[args.index("--") + 1:]
    environment = {"PATH": os.environ["PATH"]}
    for index, flag in enumerate(args):
        if flag in {"--env", "--secret-env"}:
            key, value = args[index + 1].split("=", 1)
            environment[key] = os.environ[value] if flag == "--secret-env" else value
    if command[0] == "/usr/local/bin/python3" and len(command) > 1 and command[1].endswith("oe-sandbox-transfer.py"):
        # Map the guest image's absolute interpreter to the host interpreter.
        # Do not rescue unqualified python3: it must obey the command's PATH.
        command[0] = sys.executable
        command[1] = str(guest(command[1], machine))
        if command[2] == "pack":
            command[4] = str(guest(command[4], machine))
        elif command[2] in {"unpack", "remove"}:
            command[3] = str(guest(command[3], machine))
    directory = option("--workdir") if "--workdir" in args else str(root / machine)
    os.chdir(directory)
    os.execvpe(command[0], command, environment)
else:
    raise AssertionError(action)
