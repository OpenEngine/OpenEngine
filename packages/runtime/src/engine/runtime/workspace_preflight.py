"""Bounded source inventory for workspace preparation and agent context."""

import os
from pathlib import Path
import stat
import subprocess

from engine.ports.sandbox import command_environment

# Source budgets match the SmolVM transfer limits. Transfers additionally check
# their complete archive (including Git metadata) at the sandbox boundary.
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_MEMBERS = 10_000
LARGE_FILE_BYTES = 10 * 1024 * 1024
_MARKERS = {"Cargo.toml", "Package.swift", "package.json", "pyproject.toml", "go.mod", "CMakeLists.txt"}


def _git(root: Path, *arguments: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            env={**command_environment(), "GIT_OPTIONAL_LOCKS": "0"},
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as error:
        message = "Cannot inspect workspace Git state. Select an initialized Git repository and a valid base ref."
        raise ValueError(message) from error
    return [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def inspect_workspace(root: Path, *, local_changes: bool) -> dict[str, object]:
    if not root.is_dir():
        message = "Workspace source directory does not exist; select an existing repository."
        raise ValueError(message)
    tracked = set(_git(root, "ls-files", "--cached", "-z"))
    untracked = set(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    ignored = _git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    modified = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--")
    deleted = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "--diff-filter=D", "-z", "HEAD", "--")
    total = count = 0
    directories: set[Path] = set()
    markers: list[str] = []
    largest: list[tuple[int, str]] = []
    roots: set[str] = set()
    swift = False
    for name in sorted(tracked | untracked):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or any(p.casefold() == ".git" for p in relative.parts):
            message = "Workspace contains a path outside its source tree or inside Git metadata."
            raise ValueError(message)
        path = root / relative
        if any((root / p).is_symlink() for p in relative.parents if p != Path(".")):
            message = f"Workspace file has a symlink parent: {name}. Replace it with a source file inside the repository."
            raise ValueError(message)
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISDIR(info.st_mode):
            if any(relative in Path(other).parents for other in tracked | untracked):
                continue
            message = f"Workspace directory {name} cannot be copied as a file. Git submodules are not supported."
            raise ValueError(message)
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve()
            if path.readlink().is_absolute() or not target.is_relative_to(root) or any(p.casefold() == ".git" for p in target.relative_to(root).parts):
                message = f"Workspace symlink {name} must use a relative target inside the repository, outside Git metadata."
                raise ValueError(message)
        elif not stat.S_ISREG(info.st_mode):
            message = f"Workspace entry {name} is not a regular file or symlink; remove it from the input."
            raise ValueError(message)
        count += 1
        size = info.st_size if stat.S_ISREG(info.st_mode) else 0
        total += size
        directories.update(p for p in relative.parents if p != Path("."))
        if count + len(directories) + 1 > MAX_SOURCE_MEMBERS:
            message = f"Workspace exceeds the {MAX_SOURCE_MEMBERS:,}-entry sandbox limit. Ignore generated files or select a smaller repository."
            raise ValueError(message)
        if total > MAX_SOURCE_BYTES:
            message = f"Workspace source exceeds the {MAX_SOURCE_BYTES // (1024 * 1024)} MiB sandbox limit. Ignore build outputs or large data files."
            raise ValueError(message)
        roots.add(relative.parts[0])
        if relative.name in _MARKERS and len(markers) < 20:
            markers.append(name)
        if size >= LARGE_FILE_BYTES:
            largest = sorted([*largest, (size, name)], reverse=True)[:5]
        swift = swift or relative.suffix == ".swift"
    notes = ["Inventory describes the initial run input; later nodes may change it.",
             "Use paths relative to the session working directory. sourceRoot identifies the host origin, not a guest mount."]
    if count == 0:
        notes.append("No source files are included. If existing code was expected, check the selected ref and ignore rules before proceeding.")
    if swift:
        notes.append("Swift sources detected. The SmolVM guest is Linux; macOS/AppKit UI execution requires a macOS host.")
    return {
        "sourceRoot": str(root), "inputMode": "local working files" if local_changes else "selected Git ref",
        "includedFiles": count, "sourceBytes": total, "sourceEntries": count + len(directories) + 1,
        "trackedFiles": len(tracked), "untrackedFiles": len(untracked), "modifiedFiles": len(modified),
        "deletedFiles": len(deleted), "ignoredEntries": len(ignored),
        "topLevelEntries": sorted(roots)[:20], "projectMarkers": markers,
        "largeFiles": [{"path": name, "bytes": size} for size, name in largest], "notes": notes,
    }
