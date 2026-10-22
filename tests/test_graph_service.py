"""Registered graphs, runs, loops and steering, driven over real ACP sessions.

Every agent node here is a langgraph-acp session with `graph_service_agent.py`,
a child process, and every run goes through a `LangGraphRuntime` with durable
checkpoints -- the same path the daemon takes, minus HTTP and the WorkOrder row.
"""

import asyncio
import json
import sqlite3
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
from engine.graph_runtime import GraphId, RunStatus, UnknownGraphError, UnknownRunError
from engine.graph_runtime_langgraph import (
    LangGraphRuntime,
    PullRequestRecord,
    SqliteGraphRuntimeStore,
    agent_registry,
)
from engine.cli import starters
from engine.graph_service import GraphError, GraphService, create_app, parse_graph
from engine.graph_service.service import Conflict, NotFound, ServiceError
from engine.apps.web.loops import LoopSettingsStore
from engine.ports import Workspace
from langgraph_acp import StdioACPProvider

AGENT = Path(__file__).parent / "graph_service_agent.py"
DATABASE = "graph-runs.sqlite3"

PAIR = """apiVersion: openengine.cc/v1
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
        "apiVersion": "openengine.cc/v1",
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


class Forge:
    """Just enough source control for a node to be served the repository tools."""

    async def run_git(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("not called")

    async def request_review(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("not called")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@asynccontextmanager
async def graph_service(
    tmp_path: Path, *, clock: Clock | None = None, cost: str = "0.5", **options: Any,
) -> AsyncIterator[GraphService]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    def stub(name: str) -> StdioACPProvider:
        return StdioACPProvider(name=name, command=[sys.executable, str(AGENT)], env={"STUB_COST": cost})

    registry = agent_registry([stub("stub")])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
        runtime = LangGraphRuntime(store=store, checkpointer=saver, source_control=Forge())
        service = GraphService(
            runtime, tmp_path / DATABASE,
            workspace_provider=Checkouts(tmp_path / "checkout"),
            registry=registry,
            default_repository="example/repo",
            agent_factory=lambda row: stub(row.name),
            clock=clock,
            **options,
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
                "apiVersion": "openengine.cc/v1",
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


def test_the_example_graphs_parse() -> None:
    examples = sorted((Path(__file__).parents[1] / "docs/examples/graphs").glob("*.yaml"))
    assert examples
    for example in examples:
        parse_graph(yaml.safe_load(example.read_text()), runners=["claude", "codex"])


def test_a_prompt_may_only_read_nodes_that_can_run_before_it() -> None:
    graph = {
        "apiVersion": "openengine.cc/v1",
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


def test_a_runner_input_is_not_checked_at_registration() -> None:
    parse_graph(yaml.safe_load("""apiVersion: openengine.cc/v1
name: runner-input
inputs:
  agent: {default: claude}
implementation:
  work: {agent: '${inputs.agent}', prompt: hi}
"""), runners=["codex"])


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


def test_default_python_graph_uses_module_metadata(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            graph, created = await service.add_graph("default", source=starters.source(), format="python")
            assert created and graph["name"] == starters.DEFAULT
            assert graph["description"].startswith("Implement, check CI")
            assert graph["definition"]["workflowId"] == starters.DEFAULT
            assert str(service.workflows()[0].graph_id) == graph["versionId"]
            again, created = await service.add_graph("default", source=starters.source(), format="python")
            assert not created and again["versionId"] == graph["versionId"]
        async with graph_service(tmp_path) as service:
            assert service.get_graph("default", starters.DEFAULT)["name"] == starters.DEFAULT
            assert service.workflows()[0].graph_id == graph["versionId"]

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


def test_a_work_order_can_pin_a_version_that_is_no_longer_the_latest(tmp_path: Path) -> None:
    """`work_orders.workflow = "solo@1"` starts version 1 after version 2 is added."""
    from engine.apps.web.api import work_order_graph

    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            first, _ = await service.add_graph("default", source=single("solo", "one"))
            second, _ = await service.add_graph("default", source=single("solo", "two"))
            for reference, expected in (("solo@1", first), (first["versionId"], first), ("solo", second)):
                version = work_order_graph(service.resolve, "acme/api", reference)
                assert version is not None and version.version_id == expected["versionId"]
                workflow = service.version_workflow(version.version_id)
                assert workflow is not None and str(workflow.graph_id) == expected["versionId"]
            assert service.version_workflow("gv-missing") is None

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
            assert failed["failure"]["authRequired"]["command"] == "oe agent signin stub"

    asyncio.run(scenario())


def test_a_node_names_the_runner_it_chose_before_it_finishes(tmp_path: Path) -> None:
    # The runner comes from an input, so the graph's default cannot know it; a
    # node that fails before finishing must still name the one it ran on.
    source = yaml.safe_dump({
        "apiVersion": "openengine.cc/v1",
        "name": "chosen",
        "inputs": {"runner": {"default": "stub", "choices": ["stub", "qwen"]}},
        "implementation": {"work": {"agent": "${inputs.runner}", "prompt": "AUTH ${instruction}"}},
    })

    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_agent("opencode", name="qwen", model="m", url="http://localhost:11434/v1")
            await service.add_graph("default", source=source)
            run, _ = await service.submit_run(
                project="default", graph="chosen", instruction="x", inputs={"runner": "qwen"},
            )
            failed = await settled(service, run["runId"])
            assert failed["status"] == "failed"
            assert failed["failure"]["authRequired"]["command"] == "oe agent signin qwen"
            assert {node["node"]: node["runner"] for node in failed["nodes"]}["work"] == "qwen"

    asyncio.run(scenario())


def test_an_agents_thinking_is_published_but_is_not_its_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("thinker", "THINK ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="thinker", instruction="x")
            done = await settled(service, run["runId"])
            assert done["results"] == {"work": "Let me look. echo: THINK x"}
            events = service.runtime.store.events_since(RunId(run["runId"]))
            said = [
                (event.kind.value, event.payload.get("text"))
                for event in events
                if event.node_id == "work" and (
                    event.kind.value == "thought"
                    or (event.kind.value == "transcript" and event.payload.get("role") == "assistant")
                )
            ]
            # In the order it happened, a line at a time, and never as speech.
            assert said == [
                ("transcript", "Let me look. "),
                ("thought", "Reading the prompt.\n"),
                ("thought", "Then answering."),
                ("transcript", "echo: THINK x"),
            ]

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


def test_runs_of_a_version_that_no_longer_loads_are_left_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            await service.add_graph("default", source=single("kept", "${instruction}"))
            stale, _ = await service.submit_run(project="default", graph="solo", instruction="one")
            await settled(service, stale["runId"])
            kept, _ = await service.submit_run(project="default", graph="kept", instruction="two")
            await settled(service, kept["runId"])
        # A version stored under a graph language the daemon no longer parses.
        with sqlite3.connect(tmp_path / DATABASE) as connection:
            connection.execute(
                "UPDATE cli_graph_versions SET source = replace(source, 'openengine.cc/v1', 'openengine.dev/v1') "
                "WHERE version_id = ?", (stale["versionId"],),
            )
        async with graph_service(tmp_path) as service:
            assert [run["runId"] for run in await service.list_runs("default")] == [kept["runId"]]
            with pytest.raises(NotFound, match="which this runtime does not have"):
                await service.run_json(stale["runId"])

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


def test_a_loop_shows_the_output_of_its_latest_completed_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=PAIR)
            # Each run spends $1.00, so the cap lets the first finish and stops the second.
            loop = await service.add_loop(
                project="default", graph="pair", instruction="it", every="1h", max_spend_usd=1.4,
            )
            assert (await service.loop_json(loop["loopId"]))["latestOutput"] is None

            await service.tick()
            first = (await service.loop_json(loop["loopId"]))["activeRunId"]
            run = await settled(service, first)
            assert run["output"]["node"] == "review"
            assert run["output"]["value"] == "echo: review echo: do it (plain)"

            # A run that does not complete leaves the last completed output in place.
            clock.advance(hours=1)
            await service.tick()
            second = (await service.loop_json(loop["loopId"]))["activeRunId"]
            assert second != first
            stopped = await settled(service, second)
            assert stopped["status"] == "cancelled" and stopped["output"] is None

            latest = (await service.loop_json(loop["loopId"]))["latestOutput"]
            assert latest == {"runId": first, **run["output"]}

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
            assert run["status"] == "cancelled"  # cancelled once the cap was reached
            assert run["failure"] is None
            assert [row["runId"] for row in await service.list_runs("default", status="cancelled")] == [run_id]
            assert await service.list_runs("default", status="failed") == []
            assert "review" not in run["results"]
        async with graph_service(tmp_path, clock=clock) as service:
            loop = await service.loop_json(loop["loopId"])
            assert (await service.run_json(run_id))["status"] == "cancelled"
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
            assert "oe agent signin stub" in loop["pauseReason"]

    asyncio.run(scenario())


# --- sessions -----------------------------------------------------------------


async def mcp_tools(server: dict[str, Any]) -> list[str]:
    """The tools a client launching `server` the way claude does is offered."""
    process = await asyncio.create_subprocess_exec(
        server["command"], *server["args"],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for number, method in enumerate(("initialize", "tools/list"), start=1):
            params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
            message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params if number == 1 else {}}
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        return [tool["name"] for tool in response["result"]["tools"]]
    finally:
        process.kill()
        await process.wait()


def test_a_session_is_a_run_whose_implementation_node_is_the_callers_cli(tmp_path: Path) -> None:
    from engine.runtime.config import ApprovalConfig, BashApprovalConfig

    policy = ApprovalConfig(bash=BashApprovalConfig(deny=("git push **", "rm *.txt")))

    async def scenario() -> None:
        async with graph_service(tmp_path, session_tools=("git_subcommand",), approval_policy=policy) as service:
            started = await service.start_session(agent="claude", repository="example/repo", base_ref="origin/dev")
            assert started["status"] == "starting" and started["runId"]
            deadline = asyncio.get_running_loop().time() + 30
            while (session := service.session_json(started["sessionId"]))["status"] == "starting":
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
            assert session["status"] == "ready", session
            assert session["workspace"]["path"] == str(tmp_path / "checkout")
            arguments = session["mcp"]["args"]
            assert arguments[arguments.index("--repository-tool") + 1] == "git_subcommand"
            assert "open_pull_request" not in arguments
            assert session["settings"] == {"permissions": {"deny": ["Bash(git push:*)"]}}
            assert "git_subcommand" in session["instructions"]
            assert "git_subcommand" in await mcp_tools(session["mcp"])

            ended = await service.end_session(started["sessionId"], "added the thing")
            assert ended["status"] == "ended"
            run = await settled(service, started["runId"])
            assert run["status"] == "completed" and run["results"]["implement"] == "added the thing"
            with pytest.raises(NotFound):
                service.session_json("s-missing")
            with pytest.raises(ServiceError, match="agent must be"):
                await service.start_session(agent="gemini", repository="example/repo")

    asyncio.run(scenario())


def test_each_session_agent_is_told_engines_shell_rules_in_its_own_terms() -> None:
    from engine.graph_service.session import agent_settings
    from engine.runtime.config import ApprovalConfig, BashApprovalConfig

    policy = ApprovalConfig(bash=BashApprovalConfig(deny=("git push **", "gh pr create", "rm *.txt")))
    assert agent_settings("claude", policy) == {"permissions": {"deny": ["Bash(git push:*)", "Bash(gh pr create)"]}}
    assert agent_settings("opencode", policy) == {
        "permission": {"bash": {"git push": "deny", "git push *": "deny", "gh pr create": "deny"}},
    }
    assert agent_settings("codex", policy) == {}
    assert agent_settings("opencode", ApprovalConfig(bash=BashApprovalConfig(deny=()))) == {}


# --- agents -------------------------------------------------------------------


def test_each_service_supplies_its_own_model_tiers_to_workflow_agents(tmp_path: Path) -> None:
    """Python graphs read the tiers of the service serving them, not a process-wide table."""
    from engine.graph_runtime_langgraph import AddedAgents, agent_registry, with_added_agents

    async def scenario() -> None:
        first, second = AddedAgents(), AddedAgents()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        async with graph_service(tmp_path / "a", added_agents=first,
                                 model_tiers={"stub": {"default": "fast"}}):
            async with graph_service(tmp_path / "b", added_agents=second,
                                     model_tiers={"stub": {"default": "slow"}}):
                registry = agent_registry([StdioACPProvider(name="stub", command=["true"])])
                assert with_added_agents(registry, first).model("stub", "default") == "fast"
                assert with_added_agents(registry, second).model("stub", "default") == "slow"
                assert first.model_tiers["claude"]["elevated"] == "opus"

    asyncio.run(scenario())


def test_an_added_agent_runs_graphs_and_outlives_the_daemon(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            added = await service.add_agent("claude", name="reviewer", model="opus")
            assert added["kind"] == "claude" and added["model"] == "opus" and not added["builtin"]
            assert [agent["name"] for agent in service.agents_json()] == ["reviewer", "stub"]
            assert service.agent_json("stub")["builtin"]
            with pytest.raises(Conflict, match="already exists"):
                await service.add_agent("claude", name="reviewer")
            changed = await service.add_agent("codex", name="reviewer", model="", overwrite=True)
            assert changed["kind"] == "codex" and changed["createdAt"] == added["createdAt"]

            await service.add_graph("default", source=single("solo", "check ${instruction}").replace("agent: stub", "agent: reviewer"))
            run, _ = await service.submit_run(project="default", graph="solo", instruction="it", idempotency_key="r1")
            done = await settled(service, run["runId"])
            runners = {node["node"]: node["runner"] for node in done["nodes"]}
            assert done["status"] == "completed" and runners["work"] == "reviewer"

        async with graph_service(tmp_path) as service:
            assert service.agent_json("reviewer")["kind"] == "codex"
            assert "reviewer" in service.runners()
            removed = await service.remove_agent("reviewer")
            assert removed["removed"] and "reviewer" not in service.runners()
            with pytest.raises(NotFound):
                service.agent_json("reviewer")
            with pytest.raises(Conflict, match="built in"):
                await service.remove_agent("stub")

    asyncio.run(scenario())


def test_an_added_agent_is_offered_to_workflow_files_with_its_model(tmp_path: Path) -> None:
    from engine.graph_runtime_langgraph import AddedAgents

    added = AddedAgents()

    async def scenario() -> None:
        async with graph_service(tmp_path, added_agents=added) as service:
            await service.add_agent("opencode", name="qwen", model="qwen3", url="http://localhost:11434/v1")
            assert service.added_agents() == ("qwen",)
            assert added.names == ("qwen",) and added.models == {"qwen": "engine/qwen3"}
            await service.remove_agent("qwen")
            assert service.added_agents() == () and added.names == () and added.models == {}
            await service.add_agent("claude", name="reviewer")
        # A restarted daemon offers what it stored again.
        restarted = AddedAgents()
        async with graph_service(tmp_path, added_agents=restarted):
            assert restarted.names == ("reviewer",) and restarted.models == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(("arguments", "refusal"), [
    ({"kind": "gemini"}, "kind must be one of"),
    ({"kind": "claude", "name": "Bad Name"}, "lowercase"),
    ({"kind": "claude", "name": "round-robin"}, "runner policy"),
    ({"kind": "claude", "name": "q", "url": "http://localhost:11434/v1", "model": "m"}, "only an opencode"),
    ({"kind": "opencode", "name": "q", "url": "http://localhost:11434/v1"}, "needs a model"),
    ({"kind": "opencode", "name": "q", "url": "ftp://box", "model": "m"}, "http"),
    ({"kind": "opencode", "name": "q", "url": "http://gpu:8000/v1?api_key=sk-1", "model": "m"}, "api key"),
    ({"kind": "opencode", "name": "q", "url": "http://gpu:8000/v1?x=1&API-Key=sk-1", "model": "m"}, "api key"),
    ({"kind": "opencode", "name": "q", "url": "https://gpu/v1?access_token=t", "model": "m"}, "api key"),
    ({"kind": "claude", "name": "stub"}, "built in"),
])
def test_an_agent_that_cannot_run_is_refused(tmp_path: Path, arguments: dict[str, str], refusal: str) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            kind = arguments.pop("kind")
            with pytest.raises(ServiceError, match=refusal):
                await service.add_agent(kind, **arguments)
            assert [agent["name"] for agent in service.agents_json()] == ["stub"]

    asyncio.run(scenario())


def test_a_refused_query_string_key_is_not_repeated(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            with pytest.raises(ServiceError) as refused:
                await service.add_agent("opencode", name="q", model="m", url="http://gpu:8000/v1?key=sk-secret")
            assert "sk-secret" not in repr(refused.value) and "sk-secret" not in str(vars(refused.value))

    asyncio.run(scenario())


def test_a_query_string_without_a_key_is_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            added = await service.add_agent("opencode", name="q", model="m", url="http://gpu:8000/v1?region=us")
            assert added["url"] == "http://gpu:8000/v1?region=us"

    asyncio.run(scenario())


def test_an_opencode_agent_on_a_model_server_gets_its_own_provider() -> None:
    from engine.graph_service.service import opencode_config, session_model
    from engine.graph_service.store import AgentRow

    row = AgentRow("qwen", "opencode", "qwen3-coder", "http://gpu.local:8000/v1", "", "")
    assert session_model(row) == "engine/qwen3-coder"
    assert opencode_config(row) == {
        "model": "engine/qwen3-coder",
        "provider": {"engine": {
            "npm": "@ai-sdk/openai-compatible", "name": "qwen",
            "options": {"baseURL": "http://gpu.local:8000/v1"},
            "models": {"qwen3-coder": {"name": "qwen3-coder"}},
        }},
    }
    assert opencode_config(AgentRow("o", "opencode", "", "", "", "")) == {}
    limited = AgentRow("qwen", "opencode", "qwen-agent:latest", "http://127.0.0.1:11434/v1", "", "", 65536, 8192)
    assert opencode_config(limited)["provider"]["engine"]["models"] == {
        "qwen-agent:latest": {"name": "qwen-agent:latest", "limit": {"context": 65536, "output": 8192}},
    }


def test_an_agent_on_a_model_server_keeps_its_context_limit(tmp_path: Path) -> None:
    from engine.graph_service.service import opencode_config

    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            url = "http://127.0.0.1:11434/v1"
            added = await service.add_agent("opencode", name="qwen", model="q", url=url, context_limit=65536)
            assert (added["contextLimit"], added["outputLimit"]) == (65536, None)
            row = service.store.agent("qwen")
            assert row is not None and opencode_config(row)["provider"]["engine"]["models"]["q"]["limit"] == {
                "context": 65536, "output": 0,
            }
            plain = await service.add_agent("opencode", name="plain", model="q", url=url)
            assert plain["contextLimit"] is None
            for limits in ({"context_limit": "big"}, {"context_limit": -1}, {"output_limit": 8192},
                           {"context_limit": 4096, "output_limit": 8192}):
                with pytest.raises(ServiceError):
                    await service.add_agent("opencode", name="bad", model="q", url=url, **limits)
            with pytest.raises(ServiceError):
                await service.add_agent("claude", name="bad", context_limit=65536)

    asyncio.run(scenario())


def test_an_added_agents_context_limit_reaches_the_nodes_that_run_it(tmp_path: Path) -> None:
    """The node bounds its replay by the window the agent was added with."""

    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            url = "http://127.0.0.1:11434/v1"
            await service.add_agent("opencode", name="qwen", model="q", url=url, context_limit=65536)
            graph, _ = await service.add_graph("p", source=PAIR.replace("agent: stub", "agent: qwen", 1))
            loaded = service._loaded[graph["versionId"]].workflow
            node = loaded.builder.nodes["implement"].runnable.afunc
            assert node.context_limits.get("qwen") == 65536

    asyncio.run(scenario())


def test_an_added_agents_model_applies_when_a_node_names_none_or_an_unknown_tier() -> None:
    from engine.graph_service.compile import GraphAgentNode
    from engine.graph_service.language import RunnerRule

    def node(model: str) -> GraphAgentNode:
        return GraphAgentNode(
            agent="qwen", prompt="p", cwd=".", state_key="k", rule=RunnerRule("literal", "qwen"), model_template=model,
            tiers={"claude": {"default": "sonnet"}}, agent_models={"qwen": "engine/qwen3"},
        )

    assert node("").model_for({}, "qwen") == "engine/qwen3"
    assert node("default").model_for({}, "qwen") == "engine/qwen3"
    assert node("other-model").model_for({}, "qwen") == "other-model"
    assert node("default").model_for({}, "claude") == "sonnet"


# --- HTTP ---------------------------------------------------------------------


def test_the_http_surface_reports_validation_problems_and_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            transport = httpx.ASGITransport(app=create_app(service))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                refused = await client.post("/graphs", json={"source": "apiVersion: openengine.cc/v1\nname: x\nimplementation: {}\n"})
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
                added = await client.post("/agents", json={"kind": "claude", "name": "second", "model": "opus"})
                assert added.status_code == 201 and added.json()["name"] == "second"
                assert (await client.post("/agents", json={"kind": "claude", "name": "second"})).status_code == 409
                assert (await client.post("/agents", json={"kind": "claude", "replace": "yes"})).status_code == 400
                assert [agent["name"] for agent in (await client.get("/agents")).json()["agents"]] == ["second", "stub"]
                assert (await client.get("/agents/second")).json()["model"] == "opus"
                assert (await client.delete("/agents/second")).json()["removed"]
                assert (await client.get("/agents/second")).status_code == 404

    asyncio.run(scenario())


# --- through the daemon -------------------------------------------------------


@pytest.mark.parametrize("mode,customized", [(None, False), ("fix", False), ("review", False),
                                           ("fix", True), ("review", True)])
def test_the_daemon_serves_the_graph_api_and_lists_its_runs_as_work_orders(
    tmp_path: Path, mode: str | None, customized: bool, *, workflow_app, monkeypatch
) -> None:
    """`engine-web` mounts the service at /api/v1 and starts runs as WorkOrders."""
    from unittest.mock import AsyncMock
    from langgraph_acp.readiness import AgentReadiness
    # This test replaces execution with a lambda; no real agent is involved.
    monkeypatch.setattr(GraphService, "agent_readiness", AsyncMock(return_value=AgentReadiness(True)))
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from engine.adapters.state_store.memory import InMemoryStateStore
    from web_fakes import ConcurrentRunner

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

            services = []

            def factory(runtime: LangGraphRuntime, start: Any) -> GraphService:
                service = GraphService(
                    runtime, tmp_path / DATABASE,
                    workspace_provider=Checkouts(tmp_path / "checkout"),
                    registry=registry, start=start, default_repository="example/repo",
                )

                services.append(service)
                return service

            app = workflow_app(
                InMemoryStateStore(), ConcurrentRunner(), graph_runtime=running(), graph_service=factory,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                async with app.router.lifespan_context(app):
                    if mode:
                        from engine.cli import starters
                        # Keep the shipped graph identity and input declarations;
                        # replace agent execution with one deterministic node.
                        source = starters.source(mode) + """
from dataclasses import replace
from langgraph.graph import StateGraph, START, END
from engine.graph_runtime_langgraph import State
builder = StateGraph(State)
builder.add_node("work", lambda state: {})
builder.add_edge(START, "work")
builder.add_edge("work", END)
workflow = replace(workflow, builder=builder)
"""
                        if customized:
                            source += '\nworkflow = replace(workflow, graph_id="custom-change", name="custom-change")\n'
                        added = await client.post("/api/v1/graphs", json={"source": source, "format": "python"})
                    else:
                        added = await client.post("/api/v1/graphs", json={"source": PAIR})
                    assert added.status_code == 201, added.text
                    if customized:
                        service = services[0]
                        version_id = added.json()["versionId"]
                        assert not service.claims_pull_request(version_id)
                        # A binding in another project must not affect this graph.
                        service.bind_verb("other", mode, version_id)
                        assert not service.claims_pull_request(version_id)
                        binding = await client.post(f"/api/v1/verbs/{mode}", json={"graph": "custom-change"})
                        assert binding.status_code == 200, binding.text
                        assert service.claims_pull_request(version_id)
                        invalid = await client.post("/api/v1/runs", json={
                            "graph": "custom-change", "instruction": "it", "idempotencyKey": "invalid",
                            "inputs": {"pr_url": "https://github.com/other/repo/pull/855"},
                        })
                        assert invalid.status_code == 400, invalid.text
                    run = await client.post("/api/v1/runs", json={
                        "graph": "custom-change" if customized else mode or "pair",
                        "instruction": "it", "idempotencyKey": "k",
                        "inputs": {"pr_url": "https://github.com/example/repo/pull/855"} if mode else {},
                    })
                    assert run.status_code == 201, run.text
                    run_id = run.json()["runId"]
                    assert run.json()["pullRequests"] == (
                        [{"repository": "example/repo", "number": 855}] if mode else []
                    )
                    for _ in range(600):
                        body = (await client.get(f"/api/v1/runs/{run_id}")).json()
                        if body["terminal"]:
                            break
                        await asyncio.sleep(0.05)
                    assert body["status"] == "completed", body
                    if not mode:
                        assert body["results"]["review"] == "echo: review echo: do it (plain)"
                    listed = (await client.get("/api/runs")).json()["runs"]
                    assert [item["runId"] for item in listed] == [run_id]
                    assert listed[0]["phase"] == "succeeded"
                    web_run = await client.post("/api/runs", json={
                        "workflowId": run.json()["versionId"], "prompt": "from web",
                        "repository": "example/repo",
                    })
                    assert web_run.status_code == 201, web_run.text
                    web_id = web_run.json()["runId"]
                    cli_runs = (await client.get("/api/v1/runs")).json()["runs"]
                    row = next(item for item in cli_runs if item["runId"] == web_id)
                    assert row["graphId"] is None and row["version"] is None
                    assert (await client.get(f"/api/v1/runs/{web_id}")).status_code == 200
            store.close()

    asyncio.run(scenario())


def test_the_daemon_offers_added_agents_as_workflow_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow_app,
) -> None:
    """An agent added through the graph service is a runner choice in the web UI too.

    Whoever may start a workflow may pick one, including a signed-in user who
    sees only some repositories. Only a workflow whose nodes resolve added agents offers one, and loops,
    which run on the workflow's defaults, offer none.
    """
    from dataclasses import dataclass
    from engine.apps.web.github_login import GitHubLogin
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider
    from langgraph.graph import StateGraph
    from engine.adapters.state_store.memory import InMemoryStateStore
    from engine.graph_runtime.inputs import LEAST_UTILIZED, ROUND_ROBIN, WorkflowInput
    from engine.graph_runtime_langgraph import (
        AddedAgents, State, graph_workflow, with_added_agents,
    )
    from engine.runtime.workflows import WorkflowCatalog
    from web_fakes import ConcurrentRunner

    added_agents = AddedAgents()

    @dataclass
    class Work:
        registry: Any
        graph_node_runner = "codex"
        graph_node_runner_input = "implementation_runner"

        @property
        def graph_node_runners(self) -> tuple[str, ...]:
            return self.registry.names

        async def __call__(self, state: Any) -> dict[str, Any]:
            return {}

    def runners_workflow(graph_id: str, registry: Any) -> Any:
        builder = StateGraph(State)
        builder.add_node("work", Work(registry))
        builder.set_entry_point("work")
        return graph_workflow(builder, id=graph_id, name=graph_id.title(), inputs=(
            WorkflowInput(
                "implementation_runner", "Implementation runner", "codex", True,
                ("codex", "claude", LEAST_UTILIZED, ROUND_ROBIN),
            ),
        ))

    builtin = agent_registry([CodexACPProvider(), ClaudeACPProvider()])
    workflow = runners_workflow("runners", with_added_agents(builtin, added_agents))
    own = runners_workflow("own", builtin)

    async def visible_repositories(self: GitHubLogin, request: Any) -> frozenset[str] | None:
        return frozenset({"acme/api"}) if "x-limited" in request.headers else None

    monkeypatch.setattr(GitHubLogin, "visible_repositories", visible_repositories)
    limited = {"x-limited": "1"}

    async def scenario() -> None:
        def stub(name: str) -> StdioACPProvider:
            return StdioACPProvider(name=name, command=[sys.executable, str(AGENT)])

        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
            store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
            runtime = LangGraphRuntime(workflow.compiled(saver), store=store, checkpointer=saver)

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
                    registry=agent_registry([stub("stub")]), start=start,
                    agent_factory=lambda row: stub(row.name), added_agents=added_agents,
                    default_repository="example/repo",
                )

            app = workflow_app(
                InMemoryStateStore(), ConcurrentRunner(), graph_runtime=running(), graph_service=factory,
                workflow_catalog=WorkflowCatalog.from_graphs((workflow, own)),
                loop_settings=LoopSettingsStore(tmp_path / "loops.json"),
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                async with app.router.lifespan_context(app):
                    added = await client.post("/api/v1/agents", json={
                        "kind": "opencode", "name": "qwen", "model": "qwen3", "url": "http://localhost:11434/v1",
                    })
                    assert added.status_code == 201, added.text
                    config = (await client.get("/api/config")).json()
                    choices = {
                        graph["id"]: graph["inputs"][0]["choices"] for graph in config["workflows"]
                    }
                    assert choices["runners"] == ["codex", "claude", "qwen", LEAST_UTILIZED, ROUND_ROBIN]
                    assert choices["own"] == ["codex", "claude", LEAST_UTILIZED, ROUND_ROBIN]
                    scoped = (await client.get("/api/config", headers=limited)).json()
                    assert {
                        graph["id"]: graph["inputs"][0]["choices"] for graph in scoped["workflows"]
                    } == choices
                    started = await client.post("/api/runs", json={
                        "workflowId": "runners", "repository": ".", "prompt": "Task",
                        "inputs": {"implementation_runner": "qwen"},
                    })
                    assert started.status_code == 201, started.text
                    snapshot = await runtime.snapshot(RunId(started.json()["runId"]))
                    assert snapshot.values["inputs"]["implementation_runner"] == "qwen"
                    monkeypatch.setattr("engine.apps.web.onboarding.agent_available",
                                        lambda kind, **kwargs: kind == "opencode")
                    for policy in (ROUND_ROBIN, LEAST_UTILIZED):
                        selected = await client.post("/api/runs", json={
                            "workflowId": "runners", "repository": ".", "prompt": "Task",
                            "inputs": {"implementation_runner": policy},
                        })
                        assert selected.status_code == 201, selected.text
                        snapshot = await runtime.snapshot(RunId(selected.json()["runId"]))
                        assert snapshot.values["inputs"]["implementation_runner"] == "qwen"
                    from unittest.mock import AsyncMock
                    from langgraph_acp.readiness import AgentReadiness
                    monkeypatch.setattr(GraphService, "agent_readiness",
                                        AsyncMock(return_value=AgentReadiness(False, "not signed in")))
                    registered = await client.post("/api/v1/graphs", json={
                        "project": "default", "format": "python", "source": starters.source(),
                    })
                    assert registered.status_code == 201, registered.text
                    refused = await client.post("/api/v1/runs", json={
                        "project": "default", "graph": starters.DEFAULT,
                        "instruction": "Implement the task",
                    })
                    assert refused.status_code == 400, refused.text
                    assert "oe agent signin" in refused.text
                    saved = await client.put("/api/loops/settings", json={
                        "activeHours": {"start": "00:00", "end": "00:00"}, "maxPrs": 1, "maxDailySpend": 0,
                        "runnerStrategy": "manual", "implementationRunner": "qwen", "reviewRunner": "test",
                    })
                    assert saved.status_code == 400, saved.text
            store.close()

    asyncio.run(scenario())


def test_running_graph_service_resolves_added_repository(tmp_path: Path, monkeypatch) -> None:
    from engine.runtime.repositories import RepositoryRegistry

    registry = RepositoryRegistry(projects={})
    monkeypatch.setattr(RepositoryRegistry, "_project", lambda self, path: "acme/new")

    async def scenario() -> None:
        async with graph_service(tmp_path, repositories=registry) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            registry.add("acme/new", str(tmp_path))
            assert service.resolve_repository("ACME/NEW.git") == str(tmp_path)
            run, _ = await service.submit_run(
                project="default", graph="solo", instruction="live", repository="acme/new",
            )
            snapshot = await service.runtime.snapshot(RunId(run["runId"]))
            assert snapshot.values["repository"] == str(tmp_path)
            assert (await settled(service, run["runId"]))["status"] == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("local", [False, True])
def test_v1_runs_infer_forge_mode_and_accept_local_without_a_graph_input(tmp_path, monkeypatch, local):
    from unittest.mock import AsyncMock
    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def scenario():
        async with graph_service(tmp_path) as service:
            source = service.runtime.source_control
            login = AsyncMock(return_value="bot")
            monkeypatch.setattr(source, "authenticated_login", login, raising=False)
            monkeypatch.setattr(source, "can_write_repository", AsyncMock(return_value=True), raising=False)
            app = Starlette(routes=[Mount("/api/v1", app=create_app(service))])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                added = await client.post("/api/v1/graphs", json={"source": PAIR, "format": "yaml"})
                assert added.status_code in {200, 201}, added.text
                started = await client.post("/api/v1/runs", json={
                    "graph": "pair", "instruction": "do it", "repository": "acme/api",
                    "inputs": {"mode": "disconnected"} if local else {},
                })
                assert started.status_code == 201, started.text
                context = started.json()["forgeContext"]
                assert context["mode"] == ("disconnected" if local else "connected")
                assert context["reason"]
                completed = await settled(service, started.json()["runId"])
                assert completed["forgeContext"] == context
                if local:
                    login.assert_not_called()
    asyncio.run(scenario())


PYTHON_GRAPH = '''
from engine.graph_runtime_langgraph import graph_workflow, State
from langgraph.graph import StateGraph, START, END
builder = StateGraph(State)
builder.add_node("work", lambda state: {"answer": "done"})
builder.add_edge(START, "work")
builder.add_edge("work", END)
workflow = graph_workflow(builder, id="python-example", name="Python example", description="A module description.")
'''


def test_python_registration_and_reload_do_not_block_health(tmp_path):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    async def scenario():
        async with graph_service(tmp_path) as service:
            app = Starlette(routes=[
                Route("/api/health", lambda request: JSONResponse({"ok": True})),
                Mount("/api/v1", create_app(service)),
            ])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                # Sleep represents a slow import; health must answer while it is still importing.
                task = asyncio.create_task(client.post("/api/v1/graphs", json={
                    "project": "default", "format": "python",
                    "source": "import time\ntime.sleep(0.3)\n" + PYTHON_GRAPH,
                }))
                await asyncio.sleep(0.05)
                assert (await client.get("/api/health")).status_code == 200
                assert not task.done(), "Python registration blocked health until the import finished"
                response = await task
                assert response.status_code == 201, response.text
                graph = response.json()
                assert graph["name"] == "python-example"
                assert graph["description"] == "A module description."
        # Startup uses the same off-thread loading path, and also yields to health.
        context = graph_service(tmp_path)
        reloading = asyncio.create_task(context.__aenter__())
        try:
            await asyncio.sleep(0.05)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/api/health")).status_code == 200
            assert not reloading.done(), "Python reload blocked health until the import finished"
            service = await reloading
            assert service.get_graph("default", "python-example")["versionId"] == graph["versionId"]
            run, _ = await service.submit_run(project="default", graph="python-example", instruction="go")
            assert (await settled(service, run["runId"]))["status"] == "completed"
        finally:
            await reloading
            await context.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_registration_loads_an_identical_but_unloaded_version(tmp_path):
    async def scenario():
        async with graph_service(tmp_path) as service:
            graph, _ = await service.add_graph("default", source=PAIR)
            version_id = graph["versionId"]
            service._loaded.pop(version_id)
            # Simulate a version whose original startup load failed before runtime registration.
            from unittest.mock import patch
            with patch.object(service.runtime, "register") as register:
                again, created = await service.add_graph("default", source=PAIR)
                register.assert_called_once()
            assert not created and again["versionId"] == version_id
            assert version_id in service._loaded

    asyncio.run(scenario())


def test_corrected_api_version_does_not_dedupe_to_stale_source(tmp_path):
    import hashlib
    from engine.graph_service.store import GraphRow, VersionRow, now_iso

    async def scenario():
        async with graph_service(tmp_path) as service:
            spec = parse_graph(yaml.safe_load(PAIR))
            at = now_iso()
            old_manifest = spec.json()
            old_manifest.pop("apiVersion")
            old_digest = hashlib.sha256(json.dumps(old_manifest, sort_keys=True, default=str).encode()).hexdigest()
            stale = VersionRow("gv-stale", "g-stale", 1, {}, old_digest,
                               at, source=PAIR.replace("openengine.cc/v1", "openengine.dev/v1"))
            with service.store.transaction():
                service.store.insert_graph(GraphRow("g-stale", "default", "pair", "gv-stale", at, at))
                service.store.insert_version(stale)
            corrected, created = await service.add_graph("default", source=PAIR)
            assert created and corrected["version"] == 2
            assert corrected["versionId"] != "gv-stale"
            run, _ = await service.submit_run(project="default", graph="pair", instruction="hi")
            assert (await settled(service, run["runId"]))["status"] == "completed"

    asyncio.run(scenario())


def test_onboarded_runner_survives_python_rebuild_with_session_config(tmp_path):
    async def scenario():
        async with graph_service(tmp_path, session_config={"model": "test-model"}) as service:
            graph, _ = await service.add_graph("default", source=starters.source(runner="opencode"), format="python")
            inputs = {item.name: item for item in service.workflows()[0].inputs}
            for name in ("implementation_runner", "review_runner"):
                assert inputs[name].default == "opencode"
                assert "opencode" in inputs[name].choices
            assert graph["name"] == starters.DEFAULT
    asyncio.run(scenario())


def test_runs_include_unregistered_scheduled_workorders_and_metadata_filters(tmp_path):
    from engine.graph_runtime.metadata import RunMetadata

    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.runtime.store.save_metadata(
                RunId("reserved"), GraphId("unregistered"),
                RunMetadata(name="Waiting", requester="alice", origin_channel="slack", scheduled=True),
            )
            rows = await service.list_runs(None, requester="alice", origin="slack")
            assert len(rows) == 1
            assert rows[0]["runId"] == "reserved"
            assert rows[0]["status"] == "scheduled"
            assert rows[0]["name"] == "Waiting"
            assert (await service.list_runs("default"))[0]["runId"] == "reserved"
            assert rows[0]["graphId"] is None and rows[0]["version"] is None
            assert await service.list_runs(None, requester="bob") == []
            assert await service.list_runs(None, origin="github") == []
            assert (await service.run_json("reserved"))["status"] == "scheduled"

    asyncio.run(scenario())


def typed_completion(values):
    from engine.domain import AgentRunId, StepCompleted, StepId, StepOutput

    return StepCompleted(
        run_id=RunId("typed-run"), step_id=StepId("s"),
        agent_run_id=AgentRunId("typed-agent"), outcome="success", summary="Done",
        outputs=tuple(StepOutput(name, value) for name, value in values.items()),
    )


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("kind,wire,expected", [
    ("object", '{"a": 1}', {"a": 1}),
    ("list", '[1, {"a": 2}]', [1, {"a": 2}]),
    ("number", "1.5", 1.5),
    ("integer", "1", 1),
    ("boolean", "false", False),
    ("string", '{"a": 1}', '{"a": 1}'),
    ("findings", "[]", []),
])
def test_typed_outputs_validate_and_reach_graph_state(kind, wire, expected, required):
    from engine.graph_service.compile import GraphAgentNode, _validator
    from engine.graph_service.language import OutputSpec, RunnerRule

    outputs = (OutputSpec("model", kind, required=required),)
    event = typed_completion({"model": wire})
    _validator(outputs)(event)
    node = GraphAgentNode(
        agent="stub", cwd="/tmp", state_key="s", output_key="s", rule=RunnerRule("literal", "stub"),
        outputs=outputs,
    )
    value = node._terminal_update(event)["s"]["model"]
    assert value == expected
    assert type(value) is type(expected)
    if not required:
        _validator(outputs)(typed_completion({}))
        assert "model" not in node._terminal_update(typed_completion({}))["s"]


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("kind,wire", [
    ("object", "[1]"), ("list", "{}"), ("number", "true"),
    ("integer", "1.5"), ("integer", "true"), ("boolean", "1"),
    ("object", "null"),
])
def test_typed_outputs_reject_wrong_json_type(kind, wire, required):
    from engine.graph_service.compile import _validator
    from engine.graph_service.language import OutputSpec

    with pytest.raises(ValueError, match=f"output model must be a {kind}"):
        _validator((OutputSpec("model", kind, required=required),))(
            typed_completion({"model": wire}),
        )


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("number", ["1e400", "-1e400", "NaN"])
@pytest.mark.parametrize("kind,template", [
    ("number", "NUMBER"),
    ("list", '[0, {"nested": [NUMBER]}]'),
    ("object", '{"nested": [0, {"value": NUMBER}]}'),
])
def test_typed_outputs_reject_nonfinite_numbers(kind, template, number, required):
    from engine.graph_service.compile import _validator, _wire_outputs
    from engine.graph_service.language import OutputSpec

    wire = template.replace("NUMBER", number)
    outputs = (OutputSpec("model", kind, required=required),)
    with pytest.raises(ValueError, match="output model must contain only finite numbers"):
        _wire_outputs(outputs)({"model": json.loads(wire)})
    with pytest.raises(ValueError, match="output model must contain only finite numbers"):
        _validator(outputs)(typed_completion({"model": wire}))


@pytest.mark.parametrize("kind", ["object", "list", "number", "integer", "boolean"])
@pytest.mark.parametrize("wire", ["not JSON", ""])
def test_typed_outputs_reject_invalid_json(kind, wire):
    from engine.graph_service.compile import _validator
    from engine.graph_service.language import OutputSpec

    with pytest.raises(ValueError, match=f"output model must be JSON for type {kind}:"):
        _validator((OutputSpec("model", kind),))(typed_completion({"model": wire}))


def test_typed_completion_flows_through_broker_and_downstream_nodes():
    from engine.domain import AgentId, AgentRunId, StepId, StepSpec
    from engine.graph_service.compile import GraphAgentNode, _completion_instructions, _validator, environment
    from engine.graph_service.expressions import parse, template
    from engine.graph_service.language import OutputSpec, RunnerRule
    from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry
    from langgraph.graph import END, START, StateGraph
    from engine.graph_runtime_langgraph import State
    from tests.test_terminal_mcp import _direct_request

    outputs = tuple(OutputSpec(name, kind, required=True) for name, kind in (
        ("model", "object"), ("items", "list"), ("ready", "boolean"), ("count", "number"),
    ))
    node = GraphAgentNode(
        agent="stub", cwd="/tmp", state_key="s", output_key="s", rule=RunnerRule("literal", "stub"),
        outputs=outputs,
    )
    groups = {"s": (("s",), False)}
    instructions = _completion_instructions(outputs)
    assert "native JSON values" in instructions

    async def source(state):
        broker = TerminalMcpBroker(
            run_id=RunId("typed-run"), agent_run_id=AgentRunId("typed-agent"),
            step=StepSpec(StepId("s"), AgentId("stub"), tuple(o.name for o in outputs)),
            registry=TerminalResultRegistry(), validate_completion=_validator(outputs),
        )
        async with broker:
            response = await broker._submit(_direct_request(broker, 1, "complete_step", {
                "outcome": "success", "summary": "Done", "outputs": {
                    "model": '{"a": 1}', "items": '[{"a": 1}, {"a": 2}]',
                    "ready": "false", "count": "2.5",
                },
            }))
            assert response["ok"] is True
            return node._terminal_update(await broker.result())

    def route(state):
        names = environment(state, groups)
        return "map" if parse("outputs.s.ready == false && outputs.s.count > 2").evaluate(names) else END

    def consume(state):
        names = environment(state, groups)
        assert isinstance(state["s"]["model"], dict)
        assert json.loads(template("${outputs.s.model}").render(names)) == {"a": 1}
        return {"mapped": [item["a"] for item in parse("outputs.s.items").evaluate(names)]}

    builder = StateGraph(State)
    builder.add_node("s", source)
    builder.add_node("map", consume)
    builder.add_edge(START, "s")
    builder.add_conditional_edges("s", route, ["map", END])
    builder.add_edge("map", END)
    result = asyncio.run(builder.compile().ainvoke({}))
    assert result["mapped"] == [1, 2]


@pytest.mark.parametrize("stop_during_start", [False, True])
def test_stopping_a_loop_is_permanent_even_during_start(tmp_path: Path, stop_during_start: bool) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            loop_id = loop["loopId"]
            if stop_during_start:
                start = service._start_version

                async def stop_then_start(*args, **kwargs):
                    await service.stop_loop("default", loop_id)
                    return await start(*args, **kwargs)

                service._start_version = stop_then_start
            await service.tick()
            await service.stop_loop("default", loop_id)
            stopped = await service.loop_json(loop_id)
            assert stopped["state"] == "stopped"
            assert stopped["activeRunId"] is None
            assert stopped["nextRunAt"] is None
            run_id = service.store.loop_runs(loop_id)[0][1]
            assert (await service.run_json(run_id))["status"] == "cancelled"
            for action in (service.pause_loop, service.resume_loop):
                with pytest.raises(Conflict, match="stopped loop"):
                    await action("default", loop_id)
            await service._pause(loop_id, "late failure")
            clock.advance(hours=3)
            await service.tick()
            assert (await service.loop_json(loop_id))["state"] == "stopped"
            assert len(service.store.loop_runs(loop_id)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", [UnknownRunError, UnknownGraphError])
def test_stopping_loop_clears_missing_active_run(tmp_path: Path, monkeypatch, missing) -> None:
    from unittest.mock import AsyncMock

    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            loop_id = loop["loopId"]
            service.store.update_loop(loop_id, active_run_id="run-missing")
            cancel = AsyncMock(side_effect=missing("already gone"))
            monkeypatch.setattr(service.runtime, "cancel", cancel)

            stopped = await service.stop_loop("default", loop_id)

            cancel.assert_awaited_once_with(RunId("run-missing"))
            assert stopped["state"] == "stopped"
            assert stopped["activeRunId"] is None
            assert stopped["nextRunAt"] is None
            assert service.store.loop(loop_id).active_run_id is None
            await service.stop_loop("default", loop_id)
            cancel.assert_awaited_once()
            clock.advance(hours=3)
            await service.tick()
            assert service.store.loop_runs(loop_id) == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", [UnknownRunError, UnknownGraphError])
def test_stopping_loop_during_start_clears_missing_run(tmp_path: Path, monkeypatch, missing) -> None:
    from unittest.mock import AsyncMock

    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            loop_id = loop["loopId"]

            async def stop_then_start(*args, **kwargs):
                await service.stop_loop("default", loop_id)
                return RunId("run-missing")

            monkeypatch.setattr(service, "_start_version", stop_then_start)
            cancel = AsyncMock(side_effect=missing("already gone"))
            monkeypatch.setattr(service.runtime, "cancel", cancel)
            await service.tick()

            cancel.assert_awaited_once_with(RunId("run-missing"))
            stopped = await service.loop_json(loop_id)
            assert stopped["state"] == "stopped"
            assert stopped["activeRunId"] is None
            assert stopped["nextRunAt"] is None
            assert service.store.loop(loop_id).active_run_id is None
            clock.advance(hours=3)
            await service.tick()
            assert len(service.store.loop_runs(loop_id)) == 1
            cancel.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("include_optional", [False, True])
def test_compiled_yaml_terminal_accepts_json_outputs(tmp_path, monkeypatch, include_optional):
    from types import SimpleNamespace
    from engine.graph_service.compile import compile_graph
    from engine.graph_runtime_langgraph import terminal_mcp
    from engine.runtime.terminal_mcp import TerminalMcpBroker, _mcp_response
    from tests.test_terminal_mcp import _direct_request
    from tests.test_graph_workflow_definitions import nodes_of

    spec = parse_graph({
        "apiVersion": "openengine.cc/v1", "name": "typed",
        "implementation": {"work": {"agent": "stub", "prompt": "Report values", "outputs": {
            "ready": {"type": "boolean", "required": True},
            "count": {"type": "integer", "required": True},
            "items": {"type": "list", "required": True},
            "score": {"type": "number", "required": True},
            "model": {"type": "object", "required": True},
            "note": {"type": "string", "enum": ["ok"]},
        }}},
    })
    workflow = compile_graph(spec, version_id="typed-v1", number=1,
                             workspace_provider=Checkouts(tmp_path), registry=agent_registry([]))
    node = nodes_of(workflow.builder)["work"]
    binding, = node.mcp_server_bindings
    brokers = []

    def capture(**kwargs):
        broker = TerminalMcpBroker(**kwargs)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(terminal_mcp, "TerminalMcpBroker", capture)

    async def scenario():
        execution = SimpleNamespace(run_id=RunId("typed"), execution_id="work",
                                    runtime=SimpleNamespace(source_control=Forge(), public_url=""))
        async with binding({"workspaceId": "ws"}, execution, None):
            broker, = brokers
            args = broker.config.args
            schema = json.loads(args[args.index("--outputs-schema") + 1])
            listing = await _mcp_response("", 0, "", {"id": 1, "method": "tools/list"}, outputs_schema=schema)
            assert listing["result"]["tools"][0]["inputSchema"]["properties"]["outputs"] == schema
            assert schema["properties"]["ready"]["type"] == "boolean"
            assert "note" not in schema["required"]
            values = {"ready": True, "count": 2, "items": [1, {"a": 2}], "score": 1.5, "model": {"a": 1}}
            for invalid, error in [
                ({**values, "ready": "true"}, "output ready must be a boolean"),
                ({**values, "count": True}, "output count must be a integer"),
                ({**values, "items": {}}, "output items must be a list"),
                ({**values, "note": "bad"}, "output note must be one of ok"),
                ({**values, "note": ""}, "output note must be one of ok"),
                ({**values, "score": True}, "output score must be a number"),
                ({**values, "score": json.loads("1e400")}, "output score must contain only finite numbers"),
                ({**values, "items": [0, {"a": [float("-inf")]}]}, "output items must contain only finite numbers"),
                ({**values, "model": {"a": [{"b": float("nan")}]}}, "output model must contain only finite numbers"),
                ({**values, "model": []}, "output model must be a object"),
                ({**values, "note": None}, "output note must be a string"),
                ({**values, "extra": "x"}, "undeclared outputs: extra"),
                ({k: v for k, v in values.items() if k != "ready"}, "output ready is required"),
            ]:
                response = await broker._submit(_direct_request(broker, 2, "complete_step", {
                    "outcome": "success", "summary": "Done", "outputs": invalid,
                }))
                assert response["ok"] is False
                assert error in response["error"]
                assert not broker._result.done()
            if include_optional:
                values["note"] = "ok"
            response = await broker._submit(_direct_request(broker, 3, "complete_step", {
                "outcome": "success", "summary": "Done", "outputs": values,
            }))
            assert response["ok"] is True
            state = node._terminal_update(await broker.result())["work"]
            for name, value in values.items():
                assert state[name] == value
                assert type(state[name]) is type(value)
            assert ("note" in state) == include_optional

    asyncio.run(scenario())


@pytest.mark.parametrize('only_agent', ['codex', 'claude', 'opencode'])
def test_default_workflow_can_use_one_ready_agent_for_both_stages(tmp_path, only_agent):
    from engine.graph_runtime.inputs import ROUND_ROBIN, choose_runners
    from engine.runtime.workflows import load_workflow_file

    path = tmp_path / 'starter.py'
    path.write_text(starters.source())
    graph = load_workflow_file(path)[0]
    inputs = {item.name: item.default for item in graph.inputs}
    assert inputs['implementation_runner'] == inputs['review_runner'] == ROUND_ROBIN

    async def scenario():
        async with graph_service(tmp_path) as service:
            service._registry.register(StdioACPProvider(name=only_agent, command=[sys.executable, str(AGENT)]))
            service._builtin_agents = service._registry.names
            ready = await service.ready_runner_choices(graph.inputs, inputs)
            picked = choose_runners(graph.inputs, inputs, usage=dict, turns={}, available=ready)
            assert picked['implementation_runner'] == picked['review_runner'] == only_agent
    asyncio.run(scenario())


def test_invalid_model_is_excluded_from_auto_selection_but_explicit_choice_fails(tmp_path):
    from engine.graph_runtime.inputs import ROUND_ROBIN, WorkflowInput, choose_runners
    from langgraph_acp import ACPAgentRegistry

    fake = Path(__file__).parents[1] / 'langgraph-acp/tests/fake_agent.py'
    reason = "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."
    failure = json.dumps({'type': 'error', 'status': 400,
                          'error': {'type': 'invalid_request_error', 'message': reason}})
    declarations = tuple(WorkflowInput(name, name, ROUND_ROBIN, choices=('codex', 'claude', ROUND_ROBIN))
                         for name in ('implementation_runner', 'review_runner'))
    inputs = {item.name: item.default for item in declarations}

    async def scenario():
        async with graph_service(tmp_path) as service:
            service._registry = ACPAgentRegistry([
                StdioACPProvider(name='codex', command=[sys.executable, str(fake)], env={'FAKE_AGENT_RESPONSE': failure}),
                StdioACPProvider(name='claude', command=[sys.executable, str(fake)]),
            ])
            service._builtin_agents = service._registry.names
            ready = await service.ready_runner_choices(declarations, inputs)
            chosen = choose_runners(declarations, inputs, usage=dict, turns={}, available=ready)
            assert chosen == dict.fromkeys(inputs, 'claude')
            with pytest.raises(ValueError, match='not supported when using Codex'):
                await service.ready_runner_choices(declarations, {**inputs, 'implementation_runner': 'codex'})
            service._registry.unregister('claude')
            with pytest.raises(ValueError, match='Readiness checks:.*not supported when using Codex'):
                await service.ready_runner_choices(declarations, inputs)
    asyncio.run(scenario())
