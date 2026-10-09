"""Standalone transfer helper, copied into the guest. Standard library only."""

import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

WORKSPACE = Path("/workspace")


def workspace_path(relative: str, *, destination: bool = False) -> Path:
    path = Path(relative)
    original = WORKSPACE / path
    if destination:
        _check_destination(original)
    resolved = original.resolve()
    if path.is_absolute() or not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ValueError("sandbox path must stay within its workspace")
    return resolved


def pack(source: Path, archive: Path) -> None:
    # One known root lets the receiver copy either a file or a whole directory.
    with tarfile.open(archive, "w") as output:
        output.add(source, arcname="payload", recursive=True)


def _check_destination(destination: Path) -> None:
    # Reject existing links at every destination component, including ancestors.
    for part in (destination, *destination.parents):
        if part.is_symlink():
            raise ValueError("copy destination must not contain a symlink")


def _copy(source: Path, destination: Path) -> None:
    _check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(source.readlink())
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
            _copy(child, destination / child.name)
        shutil.copystat(source, destination)
    elif destination.is_dir():
        _copy(source, destination / source.name)
    else:
        shutil.copy2(source, destination)


def unpack(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="oe-transfer-") as temporary:
        root = Path(temporary)
        try:
            with tarfile.open(archive) as incoming:
                # data filtering refuses escaping links, devices and traversal.
                incoming.extractall(root, filter="data")
            _copy(root / "payload", destination)
        finally:
            # Read-only directory modes must not prevent temporary-file cleanup,
            # including after extraction or destination validation fails.
            for directory, _, _ in os.walk(root):
                Path(directory).chmod(0o700)


def main() -> None:
    operation, *arguments = sys.argv[1:]
    try:
        if operation == "init":
            WORKSPACE.mkdir(parents=True, exist_ok=True)
        elif operation == "check":
            cwd, executable = arguments
            directory = workspace_path(cwd)
            if not directory.is_dir():
                raise FileNotFoundError("sandbox working directory does not exist")
            os.chdir(directory)
            resolved = shutil.which(executable)
            if resolved is None:
                raise FileNotFoundError("sandbox executable does not exist")
            print(json.dumps({"cwd": str(directory), "executable": resolved}))
        elif operation == "pack":
            source, archive = arguments
            pack(workspace_path(source), Path(archive))
        elif operation == "unpack":
            archive, destination = arguments
            unpack(Path(archive), workspace_path(destination, destination=True))
        elif operation == "remove":
            Path(arguments[0]).unlink(missing_ok=True)
        else:
            raise ValueError("unknown transfer operation")
    except (OSError, ValueError, tarfile.TarError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        sys.exit(2)


if __name__ == "__main__":
    main()
