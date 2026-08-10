"""Filesystem tools for workers, confined to a workspace root.

Every `path` reaching this module is model output and is treated as hostile. The
one rule: resolve to a canonical path, then verify it is still inside the root.
Resolving first is what makes it work -- it collapses `..`, follows symlinks, and
normalises separators, so the containment check sees the real destination rather
than the string the model wrote.

Doing this with string prefixes instead (`path.startswith(root)`) is the classic
way to get it wrong: `/workspace-evil` starts with `/workspace`.
"""

from collections.abc import Mapping
from pathlib import Path

from engine.ports.agent_runner import ToolResult

#: Refuse to read anything larger than this into a model's context.
MAX_READ_BYTES = 200_000

#: Cap directory listings so a huge tree cannot flood the context window.
MAX_LISTED_FILES = 500


class WorkspaceEscape(ValueError):
    """A tool call tried to reach outside the workspace root."""


class Workspace:
    """A confined view of one directory tree."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def resolve(self, relative_path: str) -> Path:
        """Resolve a model-supplied path, or refuse it.

        `strict=False` so we can resolve paths that do not exist yet (writes),
        while still collapsing traversal and symlinks before the check.
        """
        candidate = (self.root / relative_path).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceEscape(
                f"{relative_path!r} resolves outside the workspace"
            )
        return candidate

    def list_files(self, pattern: str = "*") -> list[str]:
        matches = sorted(
            str(p.relative_to(self.root))
            for p in self.root.glob(pattern)
            if p.is_file()
        )
        return matches[:MAX_LISTED_FILES]

    def read_file(self, relative_path: str) -> str:
        path = self.resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError(
                f"{relative_path} is {path.stat().st_size} bytes; "
                f"refusing to read more than {MAX_READ_BYTES}"
            )
        return path.read_text(encoding="utf-8", errors="replace")

    def write_file(self, relative_path: str, content: str) -> int:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return len(content)


async def invoke_filesystem_tool(
    workspace: Workspace, name: str, arguments: Mapping[str, object]
) -> ToolResult | None:
    """Run a worker filesystem tool. Returns None if `name` isn't one of ours.

    Errors come back as tool results rather than exceptions, so the agent can
    read what went wrong and correct itself instead of the run dying.
    """
    try:
        match name:
            case "list_files":
                pattern = str(arguments.get("pattern") or "*")
                files = workspace.list_files(pattern)
                if not files:
                    return ToolResult(f"No files match {pattern!r}.")
                return ToolResult("\n".join(files))

            case "read_file":
                return ToolResult(workspace.read_file(str(arguments["path"])))

            case "write_file":
                written = workspace.write_file(
                    str(arguments["path"]), str(arguments.get("content", ""))
                )
                return ToolResult(f"Wrote {written} bytes to {arguments['path']}.")

            case _:
                return None
    except WorkspaceEscape as error:
        return ToolResult(f"Refused: {error}", is_error=True)
    except FileNotFoundError as error:
        return ToolResult(f"No such file: {error}", is_error=True)
    except KeyError as error:
        return ToolResult(f"Missing required argument: {error}", is_error=True)
    except OSError as error:
        return ToolResult(f"Filesystem error: {error}", is_error=True)
    except ValueError as error:
        return ToolResult(str(error), is_error=True)


__all__ = [
    "MAX_LISTED_FILES",
    "MAX_READ_BYTES",
    "Workspace",
    "WorkspaceEscape",
    "invoke_filesystem_tool",
]
