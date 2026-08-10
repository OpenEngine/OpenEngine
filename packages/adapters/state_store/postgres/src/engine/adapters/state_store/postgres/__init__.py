"""State Store capability, backed by Postgres.

Placeholder for Ticket 1. Satisfies `engine.ports.StateStore` structurally; no
driver, connection pool, schema, or migrations yet.
"""

from collections.abc import Sequence

from engine.domain.events import Event
from engine.domain.ids import RunId
from engine.domain.state import RunState


class PostgresStateStore:
    """Persists run state and event history in Postgres.

    Implements `engine.ports.StateStore`.
    """

    def __init__(self, dsn: str, schema: str = "engine") -> None:
        self._dsn = dsn
        self._schema = schema

    async def load(self, run_id: RunId) -> RunState | None:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def save(self, state: RunState) -> None:
        raise NotImplementedError("Postgres writes land with the state-store ticket")

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        raise NotImplementedError("Event append lands with the state-store ticket")

    async def history(self, run_id: RunId) -> Sequence[Event]:
        raise NotImplementedError("History reads land with the state-store ticket")


__all__ = ["PostgresStateStore"]
