"""Persistence port for validated lifecycle commands and JSON snapshots.

Callers check replay before deadline/version checks or mutations, and record the
reply in the same transaction as all related state. Provider/network calls must
run outside this short transaction. Public envelope validation and authorization
remain the caller's responsibility.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

Document = dict[str, Any]
Collection = Literal["workspaces", "operations", "workspace_runtimes"]


class CommandIdConflict(ValueError):
    """An existing command ID was reused with different immutable inputs."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"COMMAND_ID_CONFLICT: {command_id}")


class Transaction(Protocol):
    async def get(self, collection: Collection, key: str) -> Document | None: ...

    async def put(self, collection: Collection, key: str, value: Document) -> None: ...

    async def all(self, collection: Collection) -> list[Document]: ...

    async def replay(self, subject: str, command: Document) -> Document | None:
        """Return the original reply, or raise CommandIdConflict for changed inputs."""
        ...

    async def record_command(self, subject: str, command: Document, response: Document) -> Document:
        """Insert an immutable receipt, or replay an existing one.

        Retryable rejections are returned without retention. On replay the
        supplied response is ignored; the first response always wins.
        """
        ...


class UnitOfWork(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """Serialize writers before reading; commit on success, roll back on any failure."""
        ...
