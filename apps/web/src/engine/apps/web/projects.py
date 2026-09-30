"""Serialize project edits with autonomous dispatch in the web service."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
import logging

from engine.domain.ids import RunId
from engine.domain.projects import Project
from engine.domain.state import RunPhase, RunState
from engine.ports.state_store import StateStore

log = logging.getLogger(__name__)


class ProjectScheduler:
    def __init__(self, store: StateStore, start: Callable[[Project], Awaitable[RunState]]) -> None:
        self.store = store
        self.start = start
        self.lock = asyncio.Lock()

    async def dispatch(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        async with self.lock:
            for project in await self.store.list_projects():
                if not project.enabled:
                    continue
                if project.last_run_id:
                    previous = await self.store.load(RunId(project.last_run_id))
                    if previous is not None and not previous.is_terminal:
                        continue
                    if previous is not None and previous.phase is RunPhase.FAILED:
                        await self.store.save_project(replace(
                            project, enabled=False, error="Previous WorkOrder failed. Review it before resuming.",
                        ))
                        continue
                if not project.eligible(now):
                    continue
                # Persist the charge and pause before calling the runtime. If the
                # process dies mid-start, restarting cannot duplicate that work.
                day = project.local_date(now)
                reserved = replace(
                    project, enabled=False, budget_date=day,
                    used_budget=(project.used_budget if project.budget_date == day else 0) + 1,
                    error="Start interrupted. Review WorkOrders before resuming.",
                )
                await self.store.save_project(reserved)
                try:
                    state = await self.start(project)
                except Exception as error:
                    log.exception("could not start project %s", project.project_id)
                    await self.store.save_project(replace(reserved, error=str(error)))
                else:
                    await self.store.save_project(replace(
                        reserved, enabled=True, last_run_id=str(state.run_id), error="",
                    ))

    async def run(self) -> None:
        while True:
            try:
                await self.dispatch()
            except Exception:
                log.exception("project dispatch failed")
            await asyncio.sleep(60)
