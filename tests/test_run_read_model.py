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
