"""Copy a local checkout's visible source into an isolated run worktree.

Read Git's file list rather than walking the directory: ignored build outputs
stay out, tracked deletions stay deleted, and the source index is never changed.
"""

import os
from pathlib import Path
import shutil
import stat
import subprocess

from engine.ports.sandbox import command_environment


def copy_local_changes(source: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        env=command_environment(), check=True, capture_output=True, timeout=120,
    )
    names = {Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}
    # Staged deletions are absent from the source index but still exist in the
    # new HEAD checkout. Include them so their files do not reappear in the run.
    baseline = subprocess.run(
        ["git", "-C", str(destination), "ls-files", "--cached", "-z"],
        env=command_environment(), check=True, capture_output=True, timeout=120,
    )
    names.update(Path(os.fsdecode(name)) for name in baseline.stdout.split(b"\0") if name)
    files: list[tuple[Path, int | None]] = []
    # Validate before copying anything. Never follow a local link outside the
    # source tree or copy Git metadata into the run's separate worktree.
    for relative in sorted(names):
        if relative.is_absolute() or any(part == ".." or part.casefold() == ".git" for part in relative.parts):
            message = f"unsafe local workspace path: {relative}"
            raise ValueError(message)
        path = source / relative
        if any((source / parent).is_symlink() for parent in relative.parents if parent != Path(".")):
            message = f"local workspace file has a symlink parent: {relative}"
            raise ValueError(message)
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            mode = None
        if mode is not None:
            if stat.S_ISLNK(mode):
                target = path.resolve()
                if not target.is_relative_to(source) or any(part.casefold() == ".git" for part in target.relative_to(source).parts):
                    message = f"local workspace symlink escapes source files: {relative}"
                    raise ValueError(message)
            elif stat.S_ISDIR(mode) and any(relative in name.parents for name in names):
                # A tracked file replaced with a directory of untracked files.
                mode = None
            elif not stat.S_ISREG(mode):
                message = f"local workspace supports files and symlinks only: {relative}"
                raise ValueError(message)
        files.append((relative, mode))
    # Delete old children before replacing a directory with a file.
    files.sort(key=lambda item: (item[1] is not None, -len(item[0].parts) if item[1] is None else len(item[0].parts)))
    for relative, mode in files:
        target = destination / relative
        if mode is None:
            if any((destination / parent).is_symlink() or (destination / parent).is_file()
                   for parent in relative.parents if parent != Path(".")):
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            continue
        # A tracked file in HEAD may now be a directory containing untracked
        # files. Remove the old file before creating those child paths.
        for parent in reversed(relative.parents):
            if parent == Path("."):
                continue
            directory = destination / parent
            if directory.is_symlink() or directory.is_file():
                directory.unlink()
            directory.mkdir(exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        shutil.copy2(source / relative, target, follow_symlinks=False)
