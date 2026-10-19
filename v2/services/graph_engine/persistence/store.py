"""The graph engine's store, as the rest of the service sees it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from openengine.persistence import Store


@dataclass(frozen=True)
class Graph:
    id: str
    name: str
    version: int
    definition: dict[str, Any]
    created_at: datetime


class Graphs(Store, Protocol):
    async def register(self, name: str, definition: dict[str, Any]) -> Graph:
        """Store `definition` as the next version of `name`, starting at 1."""
        ...

    async def get(self, id: str) -> Graph | None: ...

    async def latest(self, name: str) -> Graph | None: ...

    async def versions(self, name: str) -> list[Graph]:
        """Every version of `name`, oldest first."""
        ...
