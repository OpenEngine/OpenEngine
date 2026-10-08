"""Registered graphs, runs, loops and steering, driven over real ACP sessions.

Every agent node here is a langgraph-acp session with `graph_service_agent.py`,
a child process, and every run goes through a `LangGraphRuntime` with durable
checkpoints -- the same path the daemon takes, minus HTTP and the WorkOrder row.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from engine.domain import RunId, WorkspaceId
from engine.graph_runtime import RunStatus
from engine.graph_runtime_langgraph import (
    LangGraphRuntime,
    PullRequestRecord,
    SqliteGraphRuntimeStore,
    agent_registry,
)
from engine.graph_service import GraphError, GraphService, create_app, parse_graph
from engine.graph_service.service import Conflict, NotFound, ServiceError
from engine.ports import Workspace
from langgraph_acp import StdioACPProvider

AGENT = Path(__file__).parent / "graph_service_agent.py"
DATABASE = "graph-runs.sqlite3"

PAIR = """apiVersion: openengine.dev/v1
name: pair
description: Implement, then review.
inputs:
  tone: {default: plain}
implementation:
  implement:
    agent: stub
    prompt: do ${instruction} (${inputs.tone})
review:
  review:
    agent: stub
    prompt: review ${outputs.implement}
"""


def single(name: str, prompt: str) -> str:
    return yaml.safe_dump({
        "apiVersion": "openengine.dev/v1",
        "name": name,
        "implementation": {"work": {"agent": "stub", "prompt": prompt}},
    })


class Checkouts:
    """A workspace provider that hands every run the same empty directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def provision(self, repository: str, base_ref: str, *, co_author: str = "") -> Workspace:
        self.root.mkdir(exist_ok=True)
        return Workspace(
            workspace_id=WorkspaceId("ws-test"), root_path=str(self.root),
            repository=repository, base_ref=base_ref, ref="engine/ws-test",
        )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@asynccontextmanager
async def graph_service(
    tmp_path: Path, *, clock: Clock | None = None, cost: str = "0.5"
) -> AsyncIterator[GraphService]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    registry = agent_registry([
        StdioACPProvider(name="stub", command=[sys.executable, str(AGENT)], env={"STUB_COST": cost}),
    ])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
        runtime = LangGraphRuntime(store=store, checkpointer=saver)
        service = GraphService(
            runtime, tmp_path / DATABASE,
            workspace_provider=Checkouts(tmp_path / "checkout"),
            registry=registry,
            default_repository="example/repo",
            clock=clock,
        )
        runtime.observe(service.observe)
        await service.open(schedule=False)
        try:
            yield service
        finally:
            await service.aclose()
            await runtime.aclose()
            store.close()


async def settled(service: GraphService, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        run = await service.run_json(run_id)
        if run["terminal"]:
            return run
        assert asyncio.get_running_loop().time() < deadline, run
        await asyncio.sleep(0.05)


async def paused(service: GraphService, loop_id: str, timeout: float = 30.0) -> dict[str, Any]:
    """The loop once paused; the service settles a loop just after its run turns terminal."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        loop = await service.loop_json(loop_id)
        if loop["state"] == "paused":
            return loop
        assert asyncio.get_running_loop().time() < deadline, loop
        await asyncio.sleep(0.05)


async def running_execution(service: GraphService, run_id: str, node: str) -> dict[str, Any]:
    """The node's attempt, once its agent is mid-turn.

    Steering that arrives before the first turn starts is held until that
    turn ends, so these tests wait for the turn the steering is meant to cut.
    """
    for _ in range(600):
        for execution in (await service.run_json(run_id))["nodes"]:
            if execution["node"] == node and execution["status"] == "running" and any(
                event.kind.value == "transcript"
                and event.payload.get("role") == "user"
                and event.execution_id == execution["executionId"]
                for event in service.runtime.store.events_since(RunId(run_id))
            ):
                return execution
        await asyncio.sleep(0.05)
    raise AssertionError(f"{node} never started")


# --- graph validation ---------------------------------------------------------


def test_a_graph_reports_every_problem_at_once() -> None:
    with pytest.raises(GraphError) as raised:
        parse_graph(
            {
                "apiVersion": "openengine.dev/v1",
                "name": "Bad Name",
                "implementation": {
                    "a": {"agent": "stub", "prompt": "go"},
                    "b": {"agent": "other", "prompt": "go"},
                    "workspace": {"agent": "stub", "prompt": "x"},
                },
                "flow": ["a -> nowhere"],
                "loop": {"every": "5s"},
            },
            runners=["stub"],
        )
    paths = {problem.path for problem in raised.value.problems}
    assert {"name", "implementation.b.agent", "implementation.workspace", "flow[0]", "loop.every"} <= paths

    # Structure and references are checked after the individual fields are valid.
    cyclic = yaml.safe_load(PAIR)
    cyclic["flow"] = ["start -> implement", "implement -> review", "review -> implement"]
    with pytest.raises(GraphError, match="loops with no route out"):
        parse_graph(cyclic)
    with pytest.raises(GraphError, match="inputs.missing: not a declared input"):
        parse_graph(yaml.safe_load(single("solo", "${inputs.missing}")))


def test_a_prompt_may_only_read_nodes_that_can_run_before_it() -> None:
    graph = {
        "apiVersion": "openengine.dev/v1",
        "name": "fan",
        "implementation": {
            "a": {"agent": "stub", "prompt": "x"},
            "b": {"agent": "stub", "prompt": "${outputs.c}"},
            "c": {"agent": "stub", "prompt": "${outputs.a}"},
        },
        "flow": ["a -> [b, c]", "b -> end", "c -> end"],
    }
    with pytest.raises(GraphError, match="c cannot have run before b"):
        parse_graph(graph)


def test_the_instruction_is_builtin_and_runners_must_exist() -> None:
    parsed = parse_graph(yaml.safe_load(single("solo", "${instruction}")))
    assert parsed.inputs == ()
    assert parsed.node("work").prompt == "${instruction}"
    with pytest.raises(GraphError, match="not configured on this backend"):
        parse_graph(yaml.safe_load(single("solo", "x")), runners=["codex"])


# --- graphs and runs ----------------------------------------------------------


def test_registering_discovering_and_running_a_graph(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            graph, created = await service.add_graph("default", source=PAIR)
            assert created and graph["version"] == 1 and graph["graphId"].startswith("g-")
            again, created = await service.add_graph("default", source=PAIR)
            assert not created and again["versionId"] == graph["versionId"]
            assert [item["name"] for item in service.list_graphs("default")] == ["pair"]
            assert service.get_graph("default", "pair")["source"] == PAIR

            run, created = await service.submit_run(
                project="default", graph="pair", instruction="the thing", idempotency_key="k1",
            )
            assert created and run["versionId"] == graph["versionId"]
            replayed, created = await service.submit_run(
                project="default", graph="pair", instruction="the thing", idempotency_key="k1",
            )
            assert not created and replayed["runId"] == run["runId"]
            with pytest.raises(Conflict, match="different run request"):
                await service.submit_run(
                    project="default", graph="pair", instruction="else", idempotency_key="k1",
                )

            done = await settled(service, run["runId"])
            assert done["status"] == "completed"
            assert done["results"] == {
                "implement": "echo: do the thing (plain)",
                "review": "echo: review echo: do the thing (plain)",
            }
            nodes = {node["node"]: node for node in done["nodes"]}
            assert nodes["implement"]["status"] == "completed"
            assert nodes["implement"]["runner"] == "stub" and nodes["implement"]["attempt"] == 1
            assert done["usage"]["costUsd"] == pytest.approx(1.0)
            assert await service.list_nodes(run["runId"]) == done["nodes"]

    asyncio.run(scenario())


def test_a_new_version_does_not_change_a_run_already_pinned(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            first, _ = await service.add_graph("default", source=single("solo", "one ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="solo", instruction="x")
            second, created = await service.add_graph("default", source=single("solo", "two ${instruction}"))
            assert created and second["version"] == 2 and second["graphId"] == first["graphId"]
            assert (await settled(service, run["runId"]))["results"] == {"work": "echo: one x"}
            assert service.get_graph("default", "solo@1")["versionId"] == first["versionId"]
            later, _ = await service.submit_run(project="default", graph="solo", instruction="x")
            assert (await settled(service, later["runId"]))["results"] == {"work": "echo: two x"}

    asyncio.run(scenario())


def test_names_resolve_within_a_project_and_ambiguity_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("alpha", source=single("solo", "x"))
            await service.add_graph("beta", source=single("solo", "y"))
            assert service.get_graph("alpha", "solo")["project"] == "alpha"
            with pytest.raises(Conflict, match="ambiguous"):
                service.get_graph(None, "solo")
            with pytest.raises(NotFound):
                service.get_graph("gamma", "solo")

    asyncio.run(scenario())


def test_registered_graphs_survive_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
        async with graph_service(tmp_path) as service:
            run, _ = await service.submit_run(project="default", graph="solo", instruction="hi")
            assert (await settled(service, run["runId"]))["results"] == {"work": "echo: hi"}

    asyncio.run(scenario())


def test_a_runner_without_credentials_fails_with_a_signin_instruction(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("locked", "AUTH ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="locked", instruction="x")
            failed = await settled(service, run["runId"])
            assert failed["status"] == "failed"
            assert failed["failure"]["node"] == "work"
            assert failed["failure"]["authRequired"]["command"] == "engine runner signin stub"

    asyncio.run(scenario())


def test_runs_list_newest_first_whoever_started_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            await service.add_graph("other", source=single("solo", "${instruction}"))
            first, _ = await service.submit_run(project="default", graph="solo", instruction="one")
            await settled(service, first["runId"])
            clock.advance(minutes=1)
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            await service.tick()
            looped = (await service.loop_json(loop["loopId"]))["activeRunId"]
            clock.advance(minutes=1)
            elsewhere, _ = await service.submit_run(project="other", graph="solo", instruction="two")

            runs = await service.list_runs("default")
            assert [run["runId"] for run in runs] == [looped, first["runId"]]
            assert runs[0]["loop"] == "slow" and runs[0]["status"] == "running"
            assert runs[1]["graph"] == "solo" and runs[1]["loopId"] is None
            assert runs[1]["status"] == "completed" and runs[1]["usage"]["costUsd"] == pytest.approx(0.5)
            assert [run["runId"] for run in await service.list_runs(None)] == [
                elsewhere["runId"], looped, first["runId"],
            ]
            assert [run["runId"] for run in await service.list_runs(None, limit=1)] == [elsewhere["runId"]]
            assert [run["runId"] for run in await service.list_runs("default", graph="solo")] == [first["runId"]]
            assert [run["runId"] for run in await service.list_runs("default", loop="slow")] == [looped]
            assert [run["runId"] for run in await service.list_runs("default", status="completed")] == [
                first["runId"],
            ]
            with pytest.raises(ServiceError, match="unknown status"):
                await service.list_runs("default", status="done")
            with pytest.raises(Conflict, match="ambiguous"):
                await service.list_runs(None, graph="solo")
            await service.runtime.cancel(RunId(looped))
            await settled(service, looped)

    asyncio.run(scenario())


def test_runs_are_refused_without_their_required_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=PAIR)
            with pytest.raises(ServiceError, match="unknown workflow inputs"):
                await service.submit_run(project="default", graph="pair", instruction="x", inputs={"nope": "1"})
            with pytest.raises(ServiceError, match="instruction is required"):
                await service.submit_run(project="default", graph="pair", instruction=" ")

    asyncio.run(scenario())


# --- steering -----------------------------------------------------------------


def test_steering_reaches_the_running_attempt_and_is_applied(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="slow", instruction="x")
            execution = await running_execution(service, run["runId"], "work")
            steering, created = await service.steer(execution["executionId"], "finish now", "s1")
            assert created and steering["status"] in ("accepted", "delivered", "applied")
            again, created = await service.steer(execution["executionId"], "finish now", "s1")
            assert not created and again["steeringId"] == steering["steeringId"]

            done = await settled(service, run["runId"])
            assert done["results"] == {"work": "echo: finish now"}
            node = await service.get_node(execution["executionId"])
            [record] = node["steering"]
            assert record["status"] == "applied"
            assert record["deliveredAt"] and record["appliedAt"]
            with pytest.raises(Conflict, match="only a running attempt"):
                await service.steer(execution["executionId"], "too late", "s2")

    asyncio.run(scenario())


def test_steering_is_not_applied_by_merely_being_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="slow", instruction="x")
            execution = await running_execution(service, run["runId"], "work")
            # A steered turn that is itself cancelled was delivered, never applied.
            await service.steer(execution["executionId"], "WAIT more", "s1")
            for _ in range(200):
                [record] = (await service.get_node(execution["executionId"]))["steering"]
                if record["status"] == "delivered":
                    break
                await asyncio.sleep(0.05)
            assert record["status"] == "delivered" and record["appliedAt"] is None
            await service.runtime.cancel(RunId(run["runId"]))
            await settled(service, run["runId"])
            [record] = (await service.get_node(execution["executionId"]))["steering"]
            assert record["status"] == "delivered"
            assert (await service.get_node(execution["executionId"]))["status"] == "cancelled"

    asyncio.run(scenario())


# --- loops --------------------------------------------------------------------


def test_a_loop_needs_a_cadence_and_never_overlaps_its_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            with pytest.raises(ServiceError, match="no cadence"):
                await service.add_loop(project="default", graph="slow", instruction="x")
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            assert loop["nextRunAt"] == clock.now.isoformat(timespec="seconds")

            await asyncio.gather(service.tick(), service.tick())
            loop = await service.loop_json(loop["loopId"])
            first = loop["activeRunId"]
            assert first and loop["runs"] == 1

            clock.advance(hours=3)
            await service.tick()
            loop = await service.loop_json(loop["loopId"])
            assert loop["activeRunId"] == first and loop["runs"] == 1

            await service.runtime.cancel(RunId(first))
            await settled(service, first)
            await service.tick()
            loop = await service.loop_json(loop["loopId"])
            assert loop["runs"] == 2 and loop["activeRunId"] != first
            # The three ticks missed while the first run worked became one.
            assert loop["nextRunAt"] == (clock.now + timedelta(hours=1)).isoformat(timespec="seconds")
            await service.runtime.cancel(RunId(loop["activeRunId"]))

    asyncio.run(scenario())


def test_reaching_max_spend_stops_the_run_and_survives_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=PAIR)
            loop = await service.add_loop(
                project="default", graph="pair", instruction="x", every="1h", max_spend_usd=0.4,
            )
            assert loop["limits"]["spendScope"]
            await service.tick()
            run_id = (await service.loop_json(loop["loopId"]))["activeRunId"]
            run = await settled(service, run_id)
            assert run["status"] == "failed"  # cancelled once the cap was reached
            assert "review" not in run["results"]
        async with graph_service(tmp_path, clock=clock) as service:
            loop = await service.loop_json(loop["loopId"])
            assert loop["state"] == "paused" and loop["pauseReason"].startswith("max-spend reached")
            assert loop["spend"]["usd"] == pytest.approx(0.5)
            clock.advance(hours=2)
            await service.tick()
            assert (await service.loop_json(loop["loopId"]))["runs"] == 1
            with pytest.raises(Conflict, match="raise the limit"):
                await service.resume_loop("default", "pair")
            resumed = await service.resume_loop("default", "pair", max_spend_usd=10)
            assert resumed["state"] == "active" and resumed["limits"]["maxSpendUsd"] == 10

    asyncio.run(scenario())


def test_reaching_max_prs_pauses_the_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path, clock=Clock()) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            loop = await service.add_loop(
                project="default", graph="solo", instruction="x", every="1h", max_prs=1,
            )
            await service.tick()
            run_id = (await service.loop_json(loop["loopId"]))["activeRunId"]
            await service.runtime.store.remember_pull_request(
                PullRequestRecord("example/repo", 7, RunId(run_id), "2026-10-05T12:00:00+00:00")
            )
            await settled(service, run_id)
            loop = await paused(service, loop["loopId"])
            assert loop["prCount"] == 1
            assert loop["pauseReason"].startswith("max-prs reached")

    asyncio.run(scenario())


def test_a_loop_pauses_when_its_runner_needs_signing_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path, clock=Clock()) as service:
            await service.add_graph("default", source=single("locked", "AUTH ${instruction}"))
            loop = await service.add_loop(project="default", graph="locked", instruction="x", every="1h")
            await service.tick()
            await settled(service, (await service.loop_json(loop["loopId"]))["activeRunId"])
            loop = await paused(service, loop["loopId"])
            assert "engine runner signin stub" in loop["pauseReason"]

    asyncio.run(scenario())


# --- HTTP ---------------------------------------------------------------------


def test_the_http_surface_reports_validation_problems_and_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            transport = httpx.ASGITransport(app=create_app(service))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                refused = await client.post("/graphs", json={"source": "apiVersion: openengine.dev/v1\nname: x\nimplementation: {}\n"})
                assert refused.status_code == 400
                assert refused.json()["problems"][0]["path"] == "implementation"
                added = await client.post("/graphs", json={"project": "p", "source": PAIR})
                assert added.status_code == 201
                assert (await client.post("/graphs", json={"project": "p", "source": PAIR})).status_code == 200
                listed = await client.get("/graphs", params={"project": "p"})
                assert [graph["name"] for graph in listed.json()["graphs"]] == ["pair"]
                assert (await client.get("/graphs/pair@1", params={"project": "p"})).json()["version"] == 1
                assert (await client.get("/runs/run-missing")).status_code == 404
                assert (await client.get("/runs", params={"project": "p"})).json() == {"runs": []}
                assert (await client.get("/runs", params={"limit": "many"})).status_code == 400
                backend = (await client.get("/backend")).json()
                assert backend["runners"] == ["stub"] and backend["execution"] == "langgraph-acp"

    asyncio.run(scenario())


# --- through the daemon -------------------------------------------------------


def test_the_daemon_serves_the_graph_api_and_lists_its_runs_as_work_orders(tmp_path: Path) -> None:
    """`engine-web` mounts the service at /api/v1 and starts runs as WorkOrders."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from engine.adapters.state_store.memory import InMemoryStateStore
    from test_web_app import ConcurrentRunner, _workflow_app

    async def scenario() -> None:
        registry = agent_registry([
            StdioACPProvider(name="stub", command=[sys.executable, str(AGENT)]),
        ])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
            store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
            runtime = LangGraphRuntime(store=store, checkpointer=saver)

            @asynccontextmanager
            async def running() -> AsyncIterator[LangGraphRuntime]:
                try:
                    yield runtime
                finally:
                    await runtime.aclose()

            def factory(runtime: LangGraphRuntime, start: Any) -> GraphService:
                return GraphService(
                    runtime, tmp_path / DATABASE,
                    workspace_provider=Checkouts(tmp_path / "checkout"),
                    registry=registry, start=start, default_repository="example/repo",
                )

            app = _workflow_app(
                InMemoryStateStore(), ConcurrentRunner(), graph_runtime=running(), graph_service=factory,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                async with app.router.lifespan_context(app):
                    added = await client.post("/api/v1/graphs", json={"source": PAIR})
                    assert added.status_code == 201, added.text
                    run = await client.post("/api/v1/runs", json={
                        "graph": "pair", "instruction": "it", "idempotencyKey": "k",
                    })
                    assert run.status_code == 201, run.text
                    run_id = run.json()["runId"]
                    for _ in range(600):
                        body = (await client.get(f"/api/v1/runs/{run_id}")).json()
                        if body["terminal"]:
                            break
                        await asyncio.sleep(0.05)
                    assert body["status"] == "completed", body
                    assert body["results"]["review"] == "echo: review echo: do it (plain)"
                    listed = (await client.get("/api/runs")).json()["runs"]
                    assert [item["runId"] for item in listed] == [run_id]
            store.close()

    asyncio.run(scenario())
