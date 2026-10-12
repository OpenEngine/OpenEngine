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
        message = "sandbox path must stay within its workspace"
        raise ValueError(message)
    return resolved


def pack(source: Path, archive: Path) -> None:
    # One known root lets the receiver copy either a file or a whole directory.
    def metadata(member: tarfile.TarInfo) -> tarfile.TarInfo:
        if member.name == "payload":
            member.pax_headers = {**member.pax_headers, "openengine.basename": source.name}
        return member

    with tarfile.open(archive, "w") as output:
        output.add(source, arcname="payload", recursive=True, filter=metadata)


def _check_destination(destination: Path) -> None:
    # Reject existing links at every destination component, including ancestors.
    for part in (destination, *destination.parents):
        if part.is_symlink():
            message = "copy destination must not contain a symlink"
            raise ValueError(message)


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


def _check_links(source: Path, destination: Path, source_root: Path, destination_root: Path) -> None:
    if source.is_symlink():
        target = source.readlink()
        # Extraction validates against its temporary root, one level above
        # payload. Check both the copied tree and its final host location.
        if (target.is_absolute()
                or not source.resolve().is_relative_to(source_root)
                or not (destination.parent / target).resolve().is_relative_to(destination_root)):
            message = "copied symlink must stay within its copied tree"
            raise ValueError(message)
    elif source.is_dir():
        for child in source.iterdir():
            _check_links(child, destination / child.name, source_root, destination_root)


def unpack(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="oe-transfer-") as temporary:
        root = Path(temporary).resolve()
        try:
            with tarfile.open(archive) as incoming:
                # data filtering refuses escaping links, devices and traversal.
                incoming.extractall(root, filter="data")
                basename = incoming.getmember("payload").pax_headers.get("openengine.basename", "payload")
            if not basename or basename in {".", ".."} or any(char in basename for char in "/\\\0"):
                message = "invalid transfer basename"
                raise ValueError(message)
            source = root / "payload"
            _check_destination(destination)
            if source.is_symlink():
                message = "copied symlink must stay within its copied tree"
                raise ValueError(message)
            if not source.is_dir() and destination.is_dir():
                destination = destination / basename
            _check_links(source, destination, source, destination.resolve())
            _copy(source, destination)
        finally:
            # Read-only directory modes must not prevent temporary-file cleanup,
            # including after extraction or destination validation fails.
            for directory, _, _ in os.walk(root):
                Path(directory).chmod(0o700)


def _dispatch(operation: str, arguments: list[str]) -> None:
    if operation == "init":
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    elif operation == "check":
        cwd, executable = arguments
        directory = workspace_path(cwd)
        if not directory.is_dir():
            message = "sandbox working directory does not exist"
            raise FileNotFoundError(message)
        os.chdir(directory)
        resolved = shutil.which(executable)
        if resolved is None:
            message = "sandbox executable does not exist"
            raise FileNotFoundError(message)
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
        message = "unknown transfer operation"
        raise ValueError(message)


def main() -> None:
    operation, *arguments = sys.argv[1:]
    try:
        _dispatch(operation, arguments)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        sys.exit(2)


if __name__ == "__main__":
    main()
