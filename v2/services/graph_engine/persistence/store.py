"""The graph engine's store, as the rest of the service sees it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from openengine.persistence import Store

DEFAULT_PROJECT = "default"
"""The project a command or query naming none acts in, and the one graphs from before projects belong to."""


@dataclass(frozen=True)
class Graph:
    graph_id: str
    project: str
    name: str
    description: str | None
    latest_revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GraphRevision:
    graph_id: str
    revision: int
    format: str
    """`resource` or `python`, as the contract's `graph_format`."""
    source: str
    source_sha256: str
    created_at: datetime


@dataclass(frozen=True)
class Operation:
    operation_id: str
    project: str
    command_id: str
    operation: str
    status: str
    resources: dict[str, str]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Command:
    """What a command's receipt is keyed and compared on."""

    project: str
    """The project the command acts in; its command ID is unique only within it."""
    command_id: str
    subject: str
    fingerprint: str
    """A digest of everything a retry must repeat: project, subject, payload, expected_version and causal_path."""


@dataclass(frozen=True)
class Definition:
    """A graph as applied: its name, and the verbatim source a revision keeps."""

    name: str
    description: str | None
    format: str
    source: str

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


class CommandIdConflict(Exception):
    """The command ID already has a receipt for a different command."""

    def __init__(self, command_id: str, subject: str, operation_id: str | None) -> None:
        super().__init__(f"command {command_id} was already used for a different command in its project")
        self.command_id = command_id
        self.subject = subject
        self.operation_id = operation_id


class VersionConflict(Exception):
    """`expected_version` is not the graph's latest revision; `actual` is None when there is no such graph."""

    def __init__(self, expected: int, actual: int | None) -> None:
        super().__init__(f"expected revision {expected}, found {actual or 'no graph'}")
        self.expected = expected
        self.actual = actual


class DeadlineExceeded(Exception):
    """The command's deadline passed before it could be accepted."""

    def __init__(self, deadline: datetime, received: datetime) -> None:
        super().__init__(f"the deadline {deadline.isoformat()} passed before the command was accepted")
        self.deadline = deadline
        self.received = received


class Graphs(Store, Protocol):
    async def apply(
        self, command: Command, definition: Definition, expected_version: int | None = None,
        deadline: datetime | None = None,
    ) -> dict[str, Any]:
        """Accept `command` and return its command-response, all in one transaction.

        Everything is within `command.project`: a command ID known there returns its
        receipt's response, or raises CommandIdConflict, and the graph is the one of
        that name there. A definition identical to the latest revision is a no-op; a
        changed one adds the next revision under the same graph ID, and a new name creates the graph at 1.
        Either way an operation and the receipt are recorded. Raises VersionConflict
        when `expected_version` is not the latest revision, and DeadlineExceeded when
        `deadline` has passed by the time the command would be accepted; both record nothing.
        """
        ...

    async def reject(self, command: Command, response: dict[str, Any]) -> dict[str, Any]:
        """Record a rejection as `command`'s receipt and return it, or the receipt it already has."""
        ...

    async def graph(self, graph_id: str) -> Graph | None: ...

    async def graphs(
        self, project: str, limit: int, name_prefix: str | None = None, after: tuple[str, str] | None = None,
    ) -> list[Graph]:
        """Up to `limit` of `project`'s graphs whose names start with `name_prefix`, by name then graph ID, after `(name, graph_id)`."""
        ...

    async def revision(self, graph_id: str, revision: int) -> GraphRevision | None: ...

    async def operation(self, operation_id: str) -> Operation | None: ...
