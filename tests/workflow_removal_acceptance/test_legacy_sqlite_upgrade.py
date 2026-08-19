"""Acceptance coverage for databases written before workflow snapshots."""

import asyncio
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import engine.runtime as runtime
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import RunId, RunPhase, StepId
from engine.runtime import WorkflowCatalog, WorkflowLoadError, load_workflow_catalog


ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.workflow_removal_acceptance
FIXTURE = Path(__file__).parent / "fixtures" / "pre_workflow_definitions.sqlite3"
LEGACY_PHASES = {
    RunId("legacy-implementation-running"): {"implementing", "running_agent"},
    RunId("legacy-review-running"): {"reviewing", "running_agent"},
    RunId("legacy-awaiting-human"): {"awaiting_human_review"},
    RunId("legacy-completed-approved"): {"succeeded"},
}


def test_fixture_is_an_authentic_pre_snapshot_database() -> None:
    connection = sqlite3.connect(FIXTURE)
    try:
        rows = connection.execute(
            "SELECT run_id, state_json FROM run_states ORDER BY run_id"
        ).fetchall()
    finally:
        connection.close()

    assert {RunId(run_id) for run_id, _ in rows} == set(LEGACY_PHASES)
    assert all('"workflow_definition"' not in state_json for _, state_json in rows)


async def _durable_run_payload(store: SQLiteStateStore, run_id: RunId) -> tuple:
    instances = tuple(await store.list_instances(workflow_run_id=run_id))
    conversations = tuple(
        await store.load_conversation(instance.instance_id) for instance in instances
    )
    return tuple(await store.history(run_id)), instances, conversations


def test_current_store_reads_legacy_runs_events_and_conversations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy-readable.sqlite3"
        shutil.copyfile(FIXTURE, database)
        store = SQLiteStateStore(database)
        try:
            for run_id, accepted_phases in LEGACY_PHASES.items():
                state = await store.load(run_id)
                assert state is not None
                assert state.phase.value in accepted_phases
                assert state.workflow_definition is None
                assert len(await store.history(run_id)) >= 2

            review_instances = await store.list_instances(
                workflow_run_id=RunId("legacy-review-running")
            )
            assert {instance.workflow_step_id for instance in review_instances} == {
                StepId("implementation"),
                StepId("review"),
            }
            for instance in review_instances:
                conversation = await store.load_conversation(instance.instance_id)
                assert conversation is not None
                assert conversation.messages
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason="migration gate: snapshotless runs are not migrated by the runtime yet",
)
def test_acceptance_snapshotless_runs_are_migrated_generically_and_idempotently(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy.sqlite3"
        shutil.copyfile(FIXTURE, database)
        store = SQLiteStateStore(database)
        catalog = load_workflow_catalog(ROOT / "workflows")
        migrate = getattr(runtime, "migrate_workflow_snapshots", None)
        assert callable(migrate), "runtime must expose migrate_workflow_snapshots"
        try:
            before = {
                run_id: await _durable_run_payload(store, run_id)
                for run_id in LEGACY_PHASES
            }
            await migrate(store, catalog)
            first = {state.run_id: state for state in await store.list_runs()}
            assert all(state.workflow_definition is not None for state in first.values())
            assert first[RunId("legacy-implementation-running")].phase is RunPhase.RUNNING_AGENT
            assert first[RunId("legacy-review-running")].phase is RunPhase.RUNNING_AGENT
            after = {
                run_id: await _durable_run_payload(store, run_id)
                for run_id in LEGACY_PHASES
            }
            assert after == before

            await migrate(store, catalog)
            second = {state.run_id: state for state in await store.list_runs()}
            assert second == first

            source_definition = next(iter(catalog))
            changed_catalog = WorkflowCatalog.from_definitions(
                (replace(source_definition, version="source-file-changed"),)
            )
            await migrate(store, changed_catalog)
            third = {state.run_id: state for state in await store.list_runs()}
            assert third == first
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason="migration gate: snapshotless runs are not migrated by the runtime yet",
)
def test_acceptance_snapshotless_run_without_catalog_definition_is_actionable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy-missing-definition.sqlite3"
        shutil.copyfile(FIXTURE, database)
        store = SQLiteStateStore(database)
        migrate = getattr(runtime, "migrate_workflow_snapshots", None)
        assert callable(migrate), "runtime must expose migrate_workflow_snapshots"
        try:
            with pytest.raises(
                WorkflowLoadError,
                match=r"legacy-implementation-running.*implementation-review-v1",
            ):
                await migrate(store, WorkflowCatalog.from_definitions(()))
        finally:
            store.close()

    asyncio.run(scenario())
