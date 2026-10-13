import asyncio
from unittest.mock import patch

from engine.domain import RunId
from engine.graph_runtime.metadata import RunMetadata
from engine.graph_runtime.topology import GraphId
from engine.graph_runtime_langgraph.store import InMemoryGraphRuntimeStore
from engine.runtime.run_read_model import RunReader


def test_dependency_candidates_skip_views_for_scheduled_prerequisites() -> None:
    async def scenario():
        reader = RunReader()
        reader.store = InMemoryGraphRuntimeStore()
        for run_id, scheduled, dependency in (
            ("started", False, None),
            ("first", True, "started"),
            ("second", True, "first"),
            ("third", True, "second"),
            ("manual", True, None),
        ):
            await reader.store.save_metadata(
                RunId(run_id), GraphId("graph"), RunMetadata(
                    scheduled=scheduled,
                    depends_on_run_id=RunId(dependency) if dependency else None,
                ),
            )

        with patch.object(reader, "_state", wraps=reader._state) as project:
            candidates = await reader.dependency_candidates()
            assert [state.run_id for state in candidates] == [RunId("first")]
            assert project.await_count == 1

        await reader.store.save_metadata(
            RunId("first"), GraphId("graph"), RunMetadata(scheduled=False),
        )
        candidates = await reader.dependency_candidates()
        assert [state.run_id for state in candidates] == [RunId("second")]
        assert len(await reader.list_runs()) == 5

    asyncio.run(scenario())


def test_listing_filters_and_bounds_checkpoint_reads_and_expires_cache() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.graph_runtime import RunStatus

    async def scenario():
        reader = RunReader()
        reader.store = InMemoryGraphRuntimeStore()
        snapshot = AsyncMock(return_value=SimpleNamespace(
            status=RunStatus.COMPLETED, error=None, values={},
        ))
        reader.runtime = SimpleNamespace(snapshot=snapshot)
        for index in range(12):
            await reader.store.save_metadata(RunId(str(index)), GraphId("graph"),
                RunMetadata(repository="visible" if index % 2 else "hidden"))
        with patch("engine.runtime.run_read_model.monotonic", return_value=0):
            first = await reader.list(visible=lambda repo: repo == "visible", limit=2)
            assert [row.run_id for row in first] == ["11", "9"]
            assert snapshot.await_count == 2
            assert await reader.list(visible=lambda repo: repo == "visible", limit=2) == first
            assert snapshot.await_count == 2
            second = await reader.list(visible=lambda repo: repo == "visible", limit=2, offset=2)
            assert [row.run_id for row in second] == ["7", "5"]
            assert snapshot.await_count == 4
            # Detail reads must never use the listing cache.
            await reader.load(RunId("11"))
            assert snapshot.await_count == 5
        snapshot.return_value.status = RunStatus.RUNNING
        with patch("engine.runtime.run_read_model.monotonic", return_value=6):
            refreshed = await reader.list(visible=lambda repo: repo == "visible", limit=2)
            assert all(row.phase == "running_agent" for row in refreshed)
            assert snapshot.await_count == 7

    asyncio.run(scenario())


def test_backfill_preserves_historical_age_and_legacy_order(tmp_path) -> None:
    from datetime import UTC, datetime
    from engine.domain import RunState, TaskId, WorkflowId
    from engine.graph_runtime_langgraph.store import SqliteGraphRuntimeStore, RunRecord

    async def scenario(store):
        reader = RunReader()
        reader.store = store
        # Existing graph insertion order differs from the legacy sequence.
        await store.remember_run(RunRecord(RunId("later"), GraphId("graph")))
        await store.remember_run(RunRecord(RunId("earlier"), GraphId("graph")))
        await store.save_metadata(RunId("current"), GraphId("graph"), RunMetadata(
            created_at="2026-01-01T00:00:00+00:00", name="keep me",
        ))
        states = [RunState(run_id=RunId(name), task_id=TaskId(name), workflow_id=WorkflowId("graph"),
                           started_at=datetime(2020, 1, 1, tzinfo=UTC) if name == "dated" else None)
                  for name in ("earlier", "later", "dated", "current")]
        await reader.backfill(states)
        rows = await store.list_runs()
        assert [row.run_id for row in rows] == ["current", "dated", "later", "earlier"]
        assert rows[0].metadata.name == "keep me"
        assert rows[1].metadata.created_at == "2020-01-01T00:00:00+00:00"
        await reader.backfill(states)
        assert await store.list_runs() == rows

    asyncio.run(scenario(InMemoryGraphRuntimeStore()))
    store = SqliteGraphRuntimeStore(tmp_path / "runs.sqlite3")
    try:
        asyncio.run(scenario(store))
    finally:
        store.close()


def test_bulk_metadata_import_is_atomic_and_uses_one_transaction(tmp_path) -> None:
    import pytest
    from engine.graph_runtime_langgraph.store import SqliteGraphRuntimeStore

    store = SqliteGraphRuntimeStore(tmp_path / "runs.sqlite3")
    statements = []
    store._connection.set_trace_callback(statements.append)

    async def scenario():
        rows = [(RunId(str(index)), GraphId("graph"), RunMetadata(name=str(index))) for index in range(20)]
        original = store.save_metadata

        async def failing_save(run_id, graph_id, metadata, **kwargs):
            if run_id == "10":
                raise RuntimeError("interrupted import")
            await original(run_id, graph_id, metadata, **kwargs)

        with patch.object(store, "save_metadata", side_effect=failing_save):
            with pytest.raises(RuntimeError, match="interrupted import"):
                await store.import_metadata(rows)
        assert await store.list_runs() == ()
        statements.clear()
        await store.import_metadata(rows)
        assert len(await store.list_runs()) == 20
        assert sum(sql.startswith("SAVEPOINT") for sql in statements) == 1
        assert sum(sql.startswith("RELEASE") for sql in statements) == 1
        assert not store._connection.in_transaction

    try:
        asyncio.run(scenario())
    finally:
        store.close()
