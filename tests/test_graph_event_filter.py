from pathlib import Path

import pytest

from engine.domain import RunId
from engine.graph_runtime import EventKind, RuntimeEvent
from engine.graph_runtime_langgraph import InMemoryGraphRuntimeStore, SqliteGraphRuntimeStore


@pytest.mark.parametrize("durable", [False, True])
def test_event_kind_filter(tmp_path: Path, durable: bool) -> None:
    store = SqliteGraphRuntimeStore(tmp_path / "events.db") if durable else InMemoryGraphRuntimeStore()
    run_id = RunId("selected")
    try:
        first = store.append_event(RuntimeEvent(run_id, EventKind.RUNNER_SELECTED, {"runner": "one"}))
        unrelated = store.append_event(RuntimeEvent(run_id, EventKind.RUN_STARTED, {"large": "x" * 10000}))
        store.append_event(RuntimeEvent(RunId("other"), EventKind.RUNNER_SELECTED, {"runner": "other"}))
        last = store.append_event(RuntimeEvent(run_id, EventKind.RUNNER_SELECTED, {"runner": "two"}))
        assert store.events_since(run_id) == (first, unrelated, last)
        if durable:
            # Unrelated payloads must never be decoded, even after reopening the store.
            store._connection.execute("UPDATE events SET payload = ? WHERE sequence = ?", ("invalid JSON", unrelated.sequence))
            store.close()
            store = SqliteGraphRuntimeStore(tmp_path / "events.db")
            plan = store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM events WHERE run_id = ? AND sequence > ? AND kind = ? ORDER BY sequence",
                (str(run_id), 0, EventKind.RUNNER_SELECTED.value),
            ).fetchall()
            assert any("events_by_run_kind" in row["detail"] for row in plan)
        assert store.events_since(run_id, kind=EventKind.RUNNER_SELECTED) == (first, last)
        assert store.events_since(run_id, first.sequence, kind=EventKind.RUNNER_SELECTED) == (last,)
        assert store.events_since(RunId("missing"), kind=EventKind.RUNNER_SELECTED) == ()
    finally:
        if durable:
            store.close()


def test_node_lookup_filters_history_and_steering_skips_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from test_graph_service import graph_service, single, running_execution, settled

    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="slow", instruction="x")
            execution = await running_execution(service, run["runId"], "work")
            original = service.runtime.store.events_since

            def selections_only(run_id, cursor=0, *, kind=None):
                assert kind is EventKind.RUNNER_SELECTED
                return original(run_id, cursor, kind=kind)

            with monkeypatch.context() as patch:
                patch.setattr(service.runtime.store, "events_since", selections_only)
                assert (await service.get_node(execution["executionId"]))["runner"] == "stub"

            def no_history(*args, **kwargs):
                pytest.fail("steering must not read event history")

            with monkeypatch.context() as patch:
                patch.setattr(service.runtime.store, "events_since", no_history)
                await service.steer(execution["executionId"], "finish now", "s1")
            assert (await settled(service, run["runId"]))["results"] == {"work": "echo: finish now"}

    asyncio.run(scenario())
