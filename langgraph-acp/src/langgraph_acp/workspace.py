"""The filesystem context an ACP session is given.

Workspace configuration sits at the session boundary rather than inside an agent
provider: the same Codex or Claude installation serves every node in a graph,
and which checkout a node may read is a property of the task, not of the agent.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ACPWorkspace:
    """Where the agent works, and what else it may reach.

    Paths are stored as `str` -- a `Path` is accepted and converted, because
    these values are headed for a JSON-RPC request where `PosixPath` is not a
    thing. Nothing here touches the filesystem: whether a directory exists, and
    whether the agent advertises support for more than one root, are questions
    for the point at which a session is created.
    """

    cwd: str | None = None
    """The session's working directory. `None` leaves it to the provider."""
    additional_directories: tuple[str, ...] = ()
    """Further roots to expose, where the agent supports more than one."""

    def __post_init__(self) -> None:
        if self.cwd is not None:
            object.__setattr__(self, "cwd", os.fspath(self.cwd))
        object.__setattr__(
            self,
            "additional_directories",
            tuple(os.fspath(directory) for directory in self.additional_directories),
        )


__all__ = ["ACPWorkspace"]
