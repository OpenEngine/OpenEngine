"""State Store capability.

Durable persistence of run state and its event history. Postgres is the intended
first implementation; an in-memory dict satisfies it for tests.

`append_events` plus `load` is deliberately event-sourcing-shaped: state can
always be rebuilt by folding history through `engine.core.decide`.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from engine.domain.events import Event
from engine.domain.ids import RunId
from engine.domain.state import RunState


@runtime_checkable
class StateStore(Protocol):
    """Persists run state and the events that produced it."""

    async def load(self, run_id: RunId) -> RunState | None:
        """Return the stored state, or None if the run is unknown."""
        ...

    async def save(self, state: RunState) -> None:
        ...

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        ...

    async def history(self, run_id: RunId) -> Sequence[Event]:
        ...


__all__ = ["StateStore"]
