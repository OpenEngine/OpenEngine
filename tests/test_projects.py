"""Autonomous projects respect working hours and persist their daily allowance."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.projects import ProjectScheduler
from engine.domain.ids import RunId, TaskId, WorkflowId
from engine.domain.projects import Project
from engine.domain.state import RunPhase, RunState

MONDAY = datetime(2026, 9, 28, 10, tzinfo=UTC)


def project(**changes):
    return replace(Project("p1", "Maintenance", ".", "review", "Improve tests", enabled=True), **changes)


@pytest.mark.parametrize(("now", "eligible"), [
    (datetime(2026, 9, 28, 8, 59, tzinfo=UTC), False),
    (datetime(2026, 9, 28, 9, tzinfo=UTC), True),
    (datetime(2026, 9, 28, 17, tzinfo=UTC), False),
    (datetime(2026, 9, 27, 10, tzinfo=UTC), False),
])
def test_working_hours(now, eligible):
    assert project().eligible(now) is eligible


def test_overnight_hours_use_start_day_and_timezone():
    overnight = project(weekdays=(0,), start_time="22:00", end_time="06:00", timezone="America/Denver")
    assert overnight.eligible(datetime(2026, 9, 29, 5, tzinfo=UTC))
    assert overnight.eligible(datetime(2026, 9, 29, 11, tzinfo=UTC))
    assert not overnight.eligible(datetime(2026, 9, 29, 12, tzinfo=UTC))
    assert not overnight.eligible(datetime(2026, 9, 30, 5, tzinfo=UTC))


@pytest.mark.parametrize("changes", [
    {"daily_budget": 0}, {"daily_budget": True}, {"weekdays": ()},
    {"weekdays": (7,)}, {"start_time": "25:00"}, {"instructions": " "},
    {"start_time": "17:00"}, {"enabled": "yes"},
])
def test_invalid_project(changes):
    with pytest.raises(ValueError):
        project(**changes)


def test_sqlite_projects_survive_restart(tmp_path):
    path = tmp_path / "state.db"
    first = SQLiteStateStore(path)
    saved = project(budget_date="2026-09-28", used_budget=1, last_run_id="run-1")
    asyncio.run(first.save_project(saved))
    first.close()
    second = SQLiteStateStore(path)
    try:
        assert asyncio.run(second.list_projects()) == (saved,)
        assert asyncio.run(second.delete_project("p1"))
        assert not asyncio.run(second.list_projects())
    finally:
        second.close()


def test_dispatch_serializes_wakeups_and_resets_daily_budget():
    async def scenario():
        store = InMemoryStateStore()
        await store.save_project(project())
        async def start(value):
            state = RunState(RunId("r1"), TaskId("t1"), WorkflowId(value.workflow), phase=RunPhase.RUNNING_AGENT)
            await store.save(state)
            return state
        starter = AsyncMock(side_effect=start)
        scheduler = ProjectScheduler(store, starter)
        await asyncio.gather(scheduler.dispatch(MONDAY), scheduler.dispatch(MONDAY))
        assert starter.await_count == 1
        await scheduler.dispatch(MONDAY.replace(day=29))
        assert starter.await_count == 1  # Yesterday's work is still running.
        state = await store.load(RunId("r1"))
        await store.save(replace(state, phase=RunPhase.SUCCEEDED))
        await scheduler.dispatch(MONDAY)
        assert starter.await_count == 1  # Today's allowance is spent.
        await scheduler.dispatch(MONDAY.replace(day=29))
        assert starter.await_count == 2
        saved, = await store.list_projects()
        assert saved.used_budget == 1
        assert saved.budget_date == "2026-09-29"
    asyncio.run(scenario())


def test_start_failure_pauses_and_keeps_reserved_budget():
    async def scenario():
        store = InMemoryStateStore()
        await store.save_project(project())
        start = AsyncMock(side_effect=RuntimeError("offline"))
        scheduler = ProjectScheduler(store, start)
        await scheduler.dispatch(MONDAY)
        await scheduler.dispatch(MONDAY)
        saved, = await store.list_projects()
        assert not saved.enabled
        assert saved.used_budget == 1
        assert saved.error == "offline"
        assert start.await_count == 1
    asyncio.run(scenario())


def test_failed_workorder_pauses_even_when_daily_budget_is_spent():
    async def scenario():
        store = InMemoryStateStore()
        await store.save_project(project(last_run_id="failed", used_budget=1, budget_date="2026-09-28"))
        await store.save(RunState(RunId("failed"), TaskId("t"), WorkflowId("review"), phase=RunPhase.FAILED))
        start = AsyncMock()
        await ProjectScheduler(store, start).dispatch(MONDAY)
        saved, = await store.list_projects()
        assert not saved.enabled
        assert "failed" in saved.error
        start.assert_not_awaited()
    asyncio.run(scenario())
