"""Executor and process fencing boundaries owned by the workspace daemon."""
from typing import Protocol


class Executor(Protocol):
    progress_kinds: tuple[str, ...]
    supports_steering: bool

    async def apply_steer(self, tx, command: dict) -> None:
        """Apply under the journal transaction, with no external side effects.

        Only advertise support if effect and outcome can commit together.
        External adapters need a separate idempotent, turn-fenced protocol.
        """
        ...

    def validate(self, payload: dict) -> None: ...
    def plan(self) -> dict:
        """Allocate an execution identity without starting any process."""
        ...
    async def run(self, journal, attempt_id: str) -> None: ...
    async def fence(self, handle: dict) -> bool:
        """Confirm all executor work stopped; False must keep ownership locked."""
        ...
