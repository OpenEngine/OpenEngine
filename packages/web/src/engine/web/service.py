"""The planner service: one live foreman, driven over HTTP.

Takes an `AgentRunner` rather than building one. That is the whole reason this
lives in `packages/` instead of `apps/` -- a consumer embedding the planner
surface supplies their own backend, and nothing here can quietly prefer ours.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from engine.domain.ids import PlanId, RunId
from engine.runtime import Foreman, ForemanEvent, Workspace


class PlannerService:
    """Owns the one live planner session the UI talks to."""

    def __init__(
        self,
        runner: Any,
        *,
        workspace_root: Path,
        backend: str = "unknown",
        model: str | None = None,
    ) -> None:
        self._runner = runner
        self.workspace_root = Path(workspace_root)
        self.backend = backend
        self.model = model
        self._turn: asyncio.Task[None] | None = None
        self._counter = 0
        self._foreman = self._new_foreman()

    def _new_foreman(self) -> Foreman:
        self._counter += 1
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        return Foreman(
            self._runner,
            run_id=RunId(f"run-{self._counter}"),
            plan_id=PlanId(f"plan-{self._counter}"),
            workspace=Workspace(self.workspace_root),
            model=self.model,
        )

    @property
    def foreman(self) -> Foreman:
        return self._foreman

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def start_turn(self, text: str) -> bool:
        """Kick off a planner turn in the background. False if one is running.

        Background rather than awaited so the POST returns immediately and the
        SSE stream carries the output -- a long turn must not hold a request
        open.
        """
        if self.busy:
            return False
        self._turn = asyncio.create_task(self._foreman.send(text))
        return True

    async def reset(self) -> None:
        if self._turn is not None:
            self._turn.cancel()
        await self._foreman.close()
        self._foreman = self._new_foreman()

    def subscribe(self) -> AsyncIterator[ForemanEvent]:
        return self._foreman.subscribe()


__all__ = ["PlannerService"]
