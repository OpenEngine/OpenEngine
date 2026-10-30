"""Persistence port for validated lifecycle commands, JSON snapshots and public events.

Callers check replay before deadline/version checks or mutations, and record the
reply and any public events in the same transaction as all related state.
Provider/network calls -- including event publication -- must run outside this
short transaction. Public envelope validation and authorization remain the
caller's responsibility.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Document = dict[str, Any]
Collection = Literal["workspaces", "operations", "workspace_runtimes"]


class CommandIdConflict(ValueError):
    """An existing command ID was reused with different immutable inputs."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"COMMAND_ID_CONFLICT: {command_id}")


class InvalidEvent(ValueError):
    """An event envelope breaks the public contract, so it could never be published."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("invalid event: " + "; ".join(errors))
        self.errors = errors


class Transaction(Protocol):
    async def get(self, collection: Collection, key: str) -> Document | None: ...

    async def put(self, collection: Collection, key: str, value: Document) -> None: ...

    async def all(self, collection: Collection) -> list[Document]: ...

    async def pending_allocations(self, limit: int, after: str = "") -> list[Document]:
        """Read a bounded, indexed page; the exclusive allocation worker owns claims."""
        ...

    async def replay(self, subject: str, command: Document) -> Document | None:
        """Return the original reply, or raise CommandIdConflict for changed inputs."""
        ...

    async def record_command(self, subject: str, command: Document, response: Document) -> Document:
        """Insert an immutable receipt, or replay an existing one.

        Retryable rejections are returned without retention. On replay the
        supplied response is ignored; the first response always wins.
        """
        ...

    async def append_event(self, envelope: Document) -> None:
        """Queue an immutable public event envelope for publication after commit.

        Its partition is `resources.workspace_id`; insertion order is publication
        order within it. An envelope that breaks the public event contract
        raises InvalidEvent, and reusing an `event_id` fails the transaction. Callers
        that replayed a command must return its response without appending.
        """
        ...


class UnitOfWork(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """Serialize writers before reading; commit on success, roll back on any failure."""
        ...


@dataclass(frozen=True)
class PendingEvent:
    sequence: int
    workspace_id: str
    event_id: str
    envelope: Document


class Outbox(Protocol):
    """Committed events awaiting publication; read and marked outside lifecycle transactions."""

    async def pending_events(self, limit: int, exclude: Iterable[str] = ()) -> list[PendingEvent]:
        """Unpublished events in insertion order, leaving out the workspaces in `exclude`."""
        ...

    async def mark_published(self, sequence: int) -> None:
        """Record the broker's acknowledgement; the envelope itself never changes."""
        ...
