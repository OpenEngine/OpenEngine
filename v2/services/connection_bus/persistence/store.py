"""The connection bus's store, as the rest of the service sees it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from openengine.persistence import Store


@dataclass(frozen=True)
class Connection:
    id: str
    provider: str
    external_id: str
    name: str
    created_at: datetime


class Connections(Store, Protocol):
    async def create(self, provider: str, external_id: str, name: str) -> Connection: ...

    async def get(self, id: str) -> Connection | None: ...

    async def find(self, provider: str, external_id: str) -> Connection | None:
        """The connection an incoming webhook from `provider`'s `external_id` belongs to."""
        ...

    async def list(self) -> list[Connection]: ...
