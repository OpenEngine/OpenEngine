"""Bounded, read-only access to the host-selected repository checkout."""
from __future__ import annotations

import heapq
from pathlib import Path


REPOSITORY_TOOL_SPECS = [
    {
        "name": "list_repository_files",
        "description": "List up to 200 entries and 4000 characters from a repository directory. Paths are relative to the configured checkout; hidden paths and symlinks are excluded.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "read_repository_file",
        "description": "Read UTF-8 repository source or documentation, with line numbers. Returns up to 200 lines and 4000 characters; use start_line to continue. Hidden paths, symlinks, binary files and files over 1 MiB are excluded.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]
REPOSITORY_TOOL_NAMES = frozenset(spec["name"] for spec in REPOSITORY_TOOL_SPECS)


class RepositoryReader:
    def __init__(self, repository: str) -> None:
        self.root = Path(repository or ".").resolve()

    def _path(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("path must be a non-empty repository-relative path")
        relative = Path(value)
        if relative.is_absolute() or any(part.startswith(".") for part in relative.parts):
            raise ValueError("absolute, parent and hidden paths are not allowed")
        path = self.root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("symlinks are not allowed")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("path must stay inside the repository")
        return path

    def call(self, name: str, arguments: object) -> str:
        if name not in REPOSITORY_TOOL_NAMES:
            raise ValueError("unknown repository tool")
        allowed = {"path", "start_line"} if name == "read_repository_file" else {"path"}
        if not isinstance(arguments, dict) or set(arguments) - allowed:
            raise ValueError("unknown repository arguments")
        path = self._path(arguments.get("path", "." if name == "list_repository_files" else ""))
        if name == "list_repository_files":
            entries = heapq.nsmallest(201, (
                entry.name + ("/" if entry.is_dir() else "")
                for entry in path.iterdir()
                if not entry.name.startswith(".") and not entry.is_symlink()
                and (entry.is_file() or entry.is_dir())
            ))
            output = "\n".join(entries[:200])
            truncated = len(entries) > 200 or len(output) > 4000
            return output[:4000] + ("\n[Listing truncated]" if truncated else "")
        start = arguments.get("start_line", 1)
        if type(start) is not int or start < 1:
            raise ValueError("start_line must be a positive integer")
        if not path.is_file():
            raise ValueError("path must name a regular file")
        with path.open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or b"\0" in data:
            raise ValueError("binary files and files over 1 MiB are not supported")
        lines = data.decode("utf-8").splitlines()
        output = "\n".join(f"{index}: {line}" for index, line in enumerate(
            lines[start - 1:start + 199], start=start
        ))
        truncated = len(output) > 4000 or len(lines) >= start + 200
        return output[:4000] + ("\n[Content truncated]" if truncated else "")
