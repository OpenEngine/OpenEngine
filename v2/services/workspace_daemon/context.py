"""Task context resolved inside the daemon, never trusted from the command.

`working_directory` and `instruction_files` are root-relative paths. Each one
resolves, symlinks included, beneath the workspace root, at acceptance and
again at launch. Instruction bytes are then read through directory descriptors
that refuse every symlink, so a link swapped in after the check cannot lead out
of the root. References need an authorized artifact port and are refused.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

MAX_INSTRUCTION_BYTES = 131072


def _inside(root: Path, value: str, directory: bool) -> Path:
    path = (root / value).resolve(strict=True)
    if not path.is_relative_to(root) or (not path.is_dir() if directory else not path.is_file()):
        raise ValueError(value)  # outside the root, or the wrong type
    return path


def resolve(root: Path, context: dict[str, Any]) -> tuple[Path, list[Path]]:
    """The working directory and instruction files, each contained in `root`; raises OSError or ValueError."""
    if context["references"]:
        raise ValueError(context["references"])  # artifact references need an authorized artifact port
    return (_inside(root, context["working_directory"], True),
            [_inside(root, value, False) for value in context["instruction_files"]])


def read_instruction(root: Path, path: Path) -> str:
    """UTF-8 text of a resolved instruction file, opened beneath `root` without following any symlink."""
    parts = path.relative_to(root).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError(path)  # not a regular file
        data = file.read(MAX_INSTRUCTION_BYTES + 1)
    if len(data) > MAX_INSTRUCTION_BYTES:
        raise ValueError(path)  # over the context limit
    return data.decode("utf-8")
