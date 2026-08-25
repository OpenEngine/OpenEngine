"""The filesystem context an ACP session is given.

Workspace configuration sits at the session boundary rather than inside an agent
provider: the same Codex or Claude installation serves every node in a graph,
and which checkout a node may read is a property of the task, not of the agent.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass

from langgraph_acp._json import checked_sequence


@dataclass(frozen=True, slots=True)
class ACPWorkspace:
    """Where the agent works, and what else it may reach.

    Paths are stored as `str` -- a `Path` is accepted and converted, because
    these values are headed for a JSON-RPC request where `PosixPath` is not a
    thing. Nothing here touches the filesystem: whether a directory exists, and
    whether the agent advertises support for more than one root, are questions
    for the point at which a session is created.

    One path where a list of them belongs is refused rather than iterated. The
    resolver form a later ticket adds -- `lambda state: [state["docs_path"]]` --
    is one pair of brackets away from handing a bare string to
    `additional_directories`, and a session opened on eleven single-character
    roots is a failure that surfaces nowhere near its cause.
    """

    cwd: str | os.PathLike[str] | None = None
    """The session's working directory. `None` leaves it to the provider."""
    additional_directories: Sequence[str | os.PathLike[str]] = ()
    """Further roots to expose, where the agent supports more than one."""

    def __post_init__(self) -> None:
        if self.cwd is not None:
            object.__setattr__(self, "cwd", os.fspath(self.cwd))
        object.__setattr__(
            self,
            "additional_directories",
            tuple(
                os.fspath(directory)
                for directory in checked_sequence(
                    self.additional_directories, field="additional_directories"
                )
            ),
        )


__all__ = ["ACPWorkspace"]
