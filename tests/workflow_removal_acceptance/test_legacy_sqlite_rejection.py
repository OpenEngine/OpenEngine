"""Acceptance coverage for the explicit no-legacy-migration policy."""

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import RunId, RunState, TaskId, WorkflowId


pytestmark = pytest.mark.workflow_removal_acceptance
FIXTURE = Path(__file__).parent / "fixtures" / "pre_workflow_definitions.sqlite3"
LEGACY_RUN_IDS = {
    RunId("legacy-implementation-running"),
    RunId("legacy-review-running"),
    RunId("legacy-awaiting-human"),
    RunId("legacy-completed-approved"),
}


def test_fixture_is_an_authentic_pre_snapshot_database() -> None:
    connection = sqlite3.connect(FIXTURE)
    try:
        rows = connection.execute(
            "SELECT run_id, state_json FROM run_states ORDER BY run_id"
        ).fetchall()
    finally:
        connection.close()

    assert {RunId(run_id) for run_id, _ in rows} == LEGACY_RUN_IDS
    assert all('"workflow_definition"' not in state_json for _, state_json in rows)


def test_pre_dsl_running_states_are_explicitly_unsupported(tmp_path: Path) -> None:
    """Removing legacy phases intentionally rejects pre-DSL active runs."""

    async def scenario() -> None:
        database = tmp_path / "legacy-unsupported.sqlite3"
        shutil.copyfile(FIXTURE, database)
        store = SQLiteStateStore(database)
        try:
            with pytest.raises(ValueError, match="implementing.*RunPhase"):
                await store.load(RunId("legacy-implementation-running"))
            with pytest.raises(ValueError, match="reviewing.*RunPhase"):
                await store.load(RunId("legacy-review-running"))
        finally:
            store.close()

    asyncio.run(scenario())


def test_startup_scan_quarantines_legacy_active_rows(tmp_path: Path) -> None:
    """An incompatible legacy row must not hide current runs or abort startup."""

    async def scenario() -> tuple[RunState, ...]:
        database = tmp_path / "legacy-mixed.sqlite3"
        shutil.copyfile(FIXTURE, database)
        store = SQLiteStateStore(database)
        current = RunState(
            run_id=RunId("current-run"),
            task_id=TaskId("current-task"),
            workflow_id=WorkflowId("current-workflow-v1"),
        )
        try:
            await store.save(current)
            with pytest.warns(RuntimeWarning, match="skipping incompatible workflow run"):
                return tuple(await store.list_runs())
        finally:
            store.close()

    runs = asyncio.run(scenario())

    assert RunId("current-run") in {run.run_id for run in runs}
    assert RunId("legacy-implementation-running") not in {
        run.run_id for run in runs
    }
    assert RunId("legacy-review-running") not in {run.run_id for run in runs}
