"""The parts of the LangGraph binding the shared contract suite cannot ask for.

`tests/test_graph_runtime.py` runs the whole control surface against this
implementation, so behaviour is covered there. What is left is the seam itself:
what the binding refuses, what it reads off a compiled graph rather than being
told, and the identity it borrows from LangGraph instead of inventing.

The last of those is the load-bearing one. An `ExecutionId` *is* a LangGraph
task id, not a name this package generates and hopes stays in step -- which is
what makes two `Send`s into one node two separately addressable executions, and
what a test here checks directly rather than by inference.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from engine.graph_runtime import (
    EventKind,
    EventLog,
    GraphId,
    NodeId,
    UnknownGraphError,
)
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime_langgraph import (
    ApprovalRecord,
    InMemoryGraphRuntimeStore,
    LangGraphDefinition,
    LangGraphRuntime,
    NoExecutionError,
    RunRecord,
    SqliteGraphRuntimeStore,
    current_execution,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPContinuation

from graph_runtime_backends import State

GRAPH = GraphId("triage")
TRIAGE = NodeId("triage")
FAST = NodeId("fast")
SLOW = NodeId("slow")


async def _triage(state: dict[str, Any]) -> dict[str, Any]:
    return {"seen": state.get("size", "small")}


async def _fast(_state: dict[str, Any]) -> dict[str, Any]:
    return {"took": "fast"}


async def _slow(_state: dict[str, Any]) -> dict[str, Any]:
    return {"took": "slow"}


def _branching() -> LangGraphDefinition:
    """One node, two possible successors, and only LangGraph knows which."""
    builder: StateGraph = StateGraph(State)
    builder.add_node(str(TRIAGE), _triage)
    builder.add_node(str(FAST), _fast)
    builder.add_node(str(SLOW), _slow)
    builder.add_edge(START, str(TRIAGE))
    builder.add_conditional_edges(
        str(TRIAGE),
        lambda state: "small" if state.get("size") == "small" else "large",
        {"small": str(FAST), "large": str(SLOW)},
    )
    builder.add_edge(str(FAST), END)
    builder.add_edge(str(SLOW), END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="Triage", graph=builder.compile(checkpointer=InMemorySaver())
    )


async def _drain(runtime: LangGraphRuntime, log: EventLog, run_id: RunId) -> None:
    async with asyncio.timeout(10):
        async for event in log.stream(run_id):
            if event.kind.value in ("run.finished", "run.failed"):
                return


# --- what the binding refuses -----------------------------------------------


def test_a_graph_compiled_without_a_checkpointer_is_refused() -> None:
    """Not a convenience check: every position this runtime reports is one.

    A graph with no checkpointer would start, run, and then have no history, no
    `resume_from`, and nothing to pick back up after a restart -- a failure that
    would surface as a client asking for a checkpoint id that never existed.
    """
    builder: StateGraph = StateGraph(State)
    builder.add_node(str(FAST), _fast)
    builder.add_edge(START, str(FAST))
    builder.add_edge(str(FAST), END)

    with pytest.raises(ValueError) as refused:
        LangGraphDefinition(graph_id=GRAPH, name="Triage", graph=builder.compile())

    assert "checkpointer" in str(refused.value)


def test_a_run_of_a_graph_this_runtime_lost_is_refused_rather_than_crashing() -> None:
    """A deployment can stop defining a graph; its runs stay in the store.

    Withdrawn from the workflow directory, or renamed without retiring the id
    it had -- either way there is nothing left to read the run's position with.
    That is a refusal a control surface can answer with a 404 and a client can
    show, and it used to be a `KeyError` on the way out of `snapshot`, which is
    a 500 on every list and page the run appears in.
    """

    async def scenario() -> None:
        store = InMemoryGraphRuntimeStore()
        await store.remember_run(RunRecord(RunId("run-orphan"), GraphId("withdrawn")))
        runtime = LangGraphRuntime(_branching(), store=store)
        try:
            with pytest.raises(UnknownGraphError) as refused:
                await runtime.snapshot(RunId("run-orphan"))
        finally:
            await runtime.aclose()
        assert "withdrawn" in str(refused.value)

    asyncio.run(scenario())


def test_a_renamed_graph_still_answers_for_the_runs_it_started() -> None:
    """The reason a rename is survivable: a run remembers the id it began with.

    The graph is rebuilt under a new id, retiring the old one, sharing the
    checkpointer and the store a restart would have. Everything a client needs
    to draw the run -- its state, its history, and a topology to draw it with --
    is still answered, and the snapshot names the graph as it is called now so
    that asking for that topology finds one.
    """
    saver = InMemorySaver()
    store = InMemoryGraphRuntimeStore()

    def graph(graph_id: GraphId, previous: tuple[GraphId, ...] = ()) -> LangGraphDefinition:
        builder: StateGraph = StateGraph(State)
        builder.add_node(str(TRIAGE), _triage)
        builder.add_node(str(FAST), _fast)
        builder.add_edge(START, str(TRIAGE))
        builder.add_edge(str(TRIAGE), str(FAST))
        builder.add_edge(str(FAST), END)
        return LangGraphDefinition(
            graph_id=graph_id,
            name="Triage",
            graph=builder.compile(checkpointer=saver),
            previous_ids=previous,
        )

    async def scenario():
        runtime = LangGraphRuntime(graph(GRAPH), store=store)
        log = EventLog()
        runtime.observe(log.append)
        run = await runtime.start(GRAPH, {"size": "small"})
        await _drain(runtime, log, run.run_id)
        await runtime.aclose()

        renamed = LangGraphRuntime(
            graph(GraphId("triage-v2"), previous=(GRAPH,)), store=store
        )
        try:
            return (
                await renamed.snapshot(run.run_id),
                await renamed.history(run.run_id),
                renamed.topology(GRAPH),
            )
        finally:
            await renamed.aclose()

    snapshot, history, described = asyncio.run(scenario())

    assert snapshot is not None
    assert dict(snapshot.values)["took"] == "fast"
    assert history
    # Named as the graph is called now, because that is the id a client can ask
    # for a topology by -- and the old id is answered for the same reason.
    assert snapshot.graph_id == GraphId("triage-v2")
    assert described is not None
    assert described.graph_id == GraphId("triage-v2")


def test_a_node_outside_a_driven_run_has_no_execution() -> None:
    """A graph invoked directly is not being controlled by anything.

    An error rather than a `None` every node would have to branch on: a node
    that publishes transcript events and raises approvals has nowhere to send
    either, and finding that out at the first `emit` is finding out too late.
    """
    with pytest.raises(NoExecutionError):
        current_execution()


# --- what it reads off the graph rather than being told ---------------------


def test_topology_hides_langgraph_endpoints_and_names_the_entry_point() -> None:
    described = _branching().topology

    assert [node.node_id for node in described.nodes] == [TRIAGE, FAST, SLOW]
    assert described.entry_point == TRIAGE
    # `__start__` and `__end__` are how Pregel spells "the input channel" and
    # "nowhere left to go". Neither is a node a person can be sent back to, so
    # neither appears -- and the edge out of `__start__` becomes the entry point.
    assert not any(
        "__" in str(edge.source) or "__" in str(edge.target)
        for edge in described.edges
    )
    assert [(edge.source, edge.target, edge.condition) for edge in described.edges] == [
        (TRIAGE, FAST, "small"),
        (TRIAGE, SLOW, "large"),
    ]


def test_a_branch_is_described_statically_and_taken_by_langgraph() -> None:
    """Topology is a description; which way a run goes is the run's business.

    Both branches are in the diagram because both are reachable. Only one is in
    the history, because only one was taken -- and nothing in this package chose
    it.
    """

    async def scenario(size: str) -> tuple[list[NodeId], dict[str, Any]]:
        runtime = LangGraphRuntime(_branching())
        log = EventLog()
        runtime.observe(log.append)
        run = await runtime.start(GRAPH, {"size": size})
        await _drain(runtime, log, run.run_id)
        history = await runtime.history(run.run_id)
        final = await runtime.snapshot(run.run_id)
        await runtime.aclose()
        return [
            node for point in history for node in point.next_nodes
        ], dict(final.values)

    visited, values = asyncio.run(scenario("small"))
    assert visited == [TRIAGE, FAST]
    assert values["took"] == "fast"

    visited, values = asyncio.run(scenario("large"))
    assert visited == [TRIAGE, SLOW]
    assert values["took"] == "slow"


# --- the identity it borrows -------------------------------------------------


def test_an_execution_id_is_the_langgraph_task_id() -> None:
    """The mapping the whole design rests on, checked from inside a node.

    LangGraph gives one invocation of one node a stable task id. That id is the
    `ExecutionId` -- not a name generated alongside it, which could get out of
    step and would make two `Send`s into one node indistinguishable the moment
    it did.
    """

    async def node(_state: dict[str, Any]) -> dict[str, Any]:
        execution = current_execution()
        return {
            "execution": str(execution.execution_id),
            "task": str(get_config()["configurable"]["__pregel_task_id"]),
        }

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        builder: StateGraph = StateGraph(State)
        builder.add_node(str(FAST), node)
        builder.add_edge(START, str(FAST))
        builder.add_edge(str(FAST), END)
        runtime = LangGraphRuntime(
            LangGraphDefinition(
                graph_id=GRAPH,
                name="One node",
                graph=builder.compile(checkpointer=InMemorySaver()),
            )
        )
        log = EventLog()
        runtime.observe(log.append)
        run = await runtime.start(GRAPH, {})
        await _drain(runtime, log, run.run_id)
        final = await runtime.snapshot(run.run_id)
        started = [
            str(event.execution_id)
            for event in log.since(run.run_id)
            if event.kind.value == "node.started"
        ]
        await runtime.aclose()
        return dict(final.values), started

    values, started = asyncio.run(scenario())

    assert values["execution"] == values["task"]
    # And it is the same id the run reported as in flight, so a client that read
    # it off a snapshot can address the node that produced it.
    assert started == [values["execution"]]


# --- what a failure leaves behind --------------------------------------------


RUNTIME_LOGGER = "engine.graph_runtime_langgraph.runtime"


async def _one_node(node: Any) -> tuple[LangGraphRuntime, EventLog]:
    builder: StateGraph = StateGraph(State)
    builder.add_node(str(FAST), node)
    builder.add_edge(START, str(FAST))
    builder.add_edge(str(FAST), END)
    runtime = LangGraphRuntime(
        LangGraphDefinition(
            graph_id=GRAPH,
            name="One node",
            graph=builder.compile(checkpointer=InMemorySaver()),
        )
    )
    log = EventLog()
    runtime.observe(log.append)
    return runtime, log


def test_a_run_that_fails_writes_the_whole_failure_to_the_process_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`run.failed` is one sentence; the rest has to be somewhere.

    A client is told `str(failure)` and nothing else -- no type, no traceback,
    no chain of causes -- and that string is all the run record keeps too. For
    an agent that answered "internal error" that leaves everyone who has to fix
    it reading a message written by the thing that could not explain itself, so
    the exception is logged where an operator can see what actually raised.
    """

    async def explode(_state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the agent refused session/prompt")

    async def scenario() -> RunId:
        runtime, log = await _one_node(explode)
        run = await runtime.start(GRAPH, {})
        await _drain(runtime, log, run.run_id)
        await runtime.aclose()
        return run.run_id

    with caplog.at_level(logging.ERROR, logger=RUNTIME_LOGGER):
        run_id = asyncio.run(scenario())

    written = caplog.text
    assert "RuntimeError: the agent refused session/prompt" in written
    # In the message rather than in `extra`, because nothing that hosts this
    # runtime installs a handler that renders extra fields: a run id put there
    # is a run id nobody can read.
    assert f"graph run {run_id} (graph {GRAPH}, node {FAST}) failed" in written


def test_a_run_that_already_has_a_reason_does_not_log_a_second_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sequence a refusal produces, which is not an error at all.

    Saying no ends the run and writes the reason down, and the agent that
    refusal releases then raises on its way out. `_fail` drops that second
    answer so a client is not told two things; the log has to drop it for the
    same reason, or every refused run leaves a traceback saying a graph failed.
    """

    async def refused_then_raises(_state: dict[str, Any]) -> dict[str, Any]:
        # What `_refuse` does, in the order it does it: write the reason down,
        # tell the client, and let go of the agent that was waiting.
        execution = current_execution()
        runtime = execution.runtime
        record = await runtime.store.run(execution.run_id)
        assert record is not None
        await runtime.store.remember_run(
            replace(record, error="running the tests was not allowed")
        )
        await runtime.publish(
            execution.run_id,
            EventKind.RUN_FAILED,
            {"error": "running the tests was not allowed"},
            execution.node_id,
        )
        raise RuntimeError("the session was closed under the agent")

    async def scenario() -> str:
        runtime, log = await _one_node(refused_then_raises)
        run = await runtime.start(GRAPH, {})
        await _drain(runtime, log, run.run_id)
        failed = [
            event for event in log.since(run.run_id) if event.kind.value == "run.failed"
        ]
        await runtime.aclose()
        return str(failed[-1].payload["error"])

    with caplog.at_level(logging.ERROR, logger=RUNTIME_LOGGER):
        reported = asyncio.run(scenario())

    assert reported == "running the tests was not allowed"
    assert caplog.records == []


# --- the durable half --------------------------------------------------------


def test_the_sqlite_store_round_trips_everything_a_restart_needs(
    tmp_path: Path,
) -> None:
    """Written by one connection, read by another, as a restart does it."""
    continuation = ACPContinuation(
        agent="codex",
        session_id="sess_abc123",
        thread_id="run-1",
        session_key="implementation",
        metadata={"graph_runtime.approval_id": "approval-1"},
    )
    record = ApprovalRecord(
        approval_id=ApprovalId("approval-1"),
        run_id=RunId("run-1"),
        execution_id=ExecutionId("task-1"),
        node_id=NodeId("implementation"),
        kind=ApprovalKind.COMMAND_EXECUTION,
        reason="run the tests",
        command="pytest",
        tool_name="execute",
        session_key="implementation",
        continuation=continuation,
        request={"sessionId": "sess_abc123"},
    )

    async def scenario() -> dict[str, Any]:
        writing = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
        await writing.remember_run(RunRecord(RunId("run-1"), GraphId("triage")))
        await writing.remember_session(
            RunId("run-1"), "implementation", continuation
        )
        await writing.remember_approval(record)
        writing.close()

        reading = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
        found = {
            "run": await reading.run(RunId("run-1")),
            "session": await reading.session(RunId("run-1"), "implementation"),
            "approval": await reading.approval(ApprovalId("approval-1")),
            "pending": await reading.pending_approvals(RunId("run-1")),
        }
        await reading.resolve_approval(
            ApprovalId("approval-1"), ApprovalDecision.ACCEPT
        )
        found["after"] = await reading.approval(ApprovalId("approval-1"))
        found["still_pending"] = await reading.pending_approvals(RunId("run-1"))
        reading.close()
        return found

    found = asyncio.run(scenario())

    assert found["run"] == RunRecord(RunId("run-1"), GraphId("triage"))
    assert found["session"] == continuation
    assert found["approval"] == record
    assert found["pending"] == (record,)
    # Answered, and no longer pending -- which is what makes a second answer a
    # race that lost rather than a request that never existed.
    assert found["after"].decision is ApprovalDecision.ACCEPT
    assert found["still_pending"] == ()


@pytest.mark.parametrize("store_factory", ["memory", "sqlite"])
def test_comments_a_run_posted_are_kept_for_the_runs_after_it(
    tmp_path: Path, store_factory: str
) -> None:
    from engine.graph_runtime_langgraph.store import CommentRecord

    posted = CommentRecord(
        comment_id=123,
        repository="acme/api",
        kind="issue",
        pr_number=42,
        run_id=RunId("run-1"),
        posted_at="2026-09-10T18:00:00+00:00",
        node_id=NodeId("reranker"),
        url="https://github.com/acme/api/pull/42#issuecomment-123",
    )
    # Same number, another id space: GitHub hands review comments out from a
    # counter of their own, and this is a different comment.
    inline = CommentRecord(
        comment_id=123,
        repository="acme/api",
        kind="review",
        pr_number=42,
        run_id=RunId("run-1"),
        posted_at="2026-09-10T18:00:30+00:00",
        url="https://github.com/acme/api/pull/42#discussion_r123",
    )
    elsewhere = CommentRecord(
        comment_id=124,
        repository="acme/web",
        kind="issue",
        pr_number=7,
        run_id=RunId("run-2"),
        posted_at="2026-09-10T18:01:00+00:00",
    )

    async def scenario() -> tuple[CommentRecord, ...]:
        path = tmp_path / "runtime.db"
        store = (
            InMemoryGraphRuntimeStore()
            if store_factory == "memory"
            else SqliteGraphRuntimeStore(path)
        )
        await store.remember_comment(posted)
        await store.remember_comment(inline)
        await store.remember_comment(elsewhere)
        if store_factory == "sqlite":
            store.close()
            store = SqliteGraphRuntimeStore(path)
        found = await store.comments(RunId("run-1"))
        # A comment posted twice is one comment: the forge's id owns the row.
        await store.remember_comment(posted)
        assert await store.comments(RunId("run-1")) == found
        assert await store.comments(RunId("run-2")) == (elsewhere,)
        # The same provenance read the other way: a webhook holding a pull
        # request finds its run without scanning every run that ever existed.
        assert await store.run_for_pull_request("acme/api", 42) == RunId("run-1")
        assert await store.run_for_pull_request("acme/web", 7) == RunId("run-2")
        # The repository is part of the question: two forges number their pull
        # requests from counters of their own.
        assert await store.run_for_pull_request("acme/web", 42) is None
        assert await store.run_for_pull_request("acme/other", 42) is None
        # A pull request opened by hand belongs to no run, and says so.
        assert await store.run_for_pull_request("acme/api", 999) is None
        # A second run taking the pull request over is the one still working.
        await store.remember_comment(replace(
            inline, comment_id=200, run_id=RunId("run-3"),
            posted_at="2026-09-10T19:00:00+00:00",
        ))
        assert await store.run_for_pull_request("acme/api", 42) == RunId("run-3")
        return found

    assert asyncio.run(scenario()) == (posted, inline)


def test_auto_approve_keeps_human_requests_manual() -> None:
    from httpx import ASGITransport, AsyncClient
    from engine.graph_runtime.api import create_app

    async def exercise() -> None:
        async def ask(_state: dict[str, Any]) -> dict[str, Any]:
            execution = current_execution()
            for kind in (
                ApprovalKind.COMMAND_EXECUTION,
                ApprovalKind.FILE_CHANGE,
                ApprovalKind.PLAN_APPROVAL,
                ApprovalKind.USER_INPUT,
                ApprovalKind.TOOL_USE,
            ):
                await execution.ask(reason=kind.value, kind=kind)
            return {}

        builder = StateGraph(State)
        builder.add_node(str(TRIAGE), ask)
        builder.add_edge(START, str(TRIAGE))
        builder.add_edge(str(TRIAGE), END)
        runtime = LangGraphRuntime(LangGraphDefinition(
            graph_id=GRAPH, name="Triage", graph=builder.compile(checkpointer=InMemorySaver())
        ))
        app = create_app(runtime)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            run = await runtime.start(GRAPH, {})

            async def pending(kind: ApprovalKind):
                async with asyncio.timeout(5):
                    while True:
                        snapshot = await runtime.snapshot(run.run_id)
                        if snapshot.pending_approvals and snapshot.pending_approvals[0].kind == kind:
                            return snapshot.pending_approvals[0]
                        await asyncio.sleep(0.001)

            url = f"/api/runs/{run.run_id}/auto-approve"
            await pending(ApprovalKind.COMMAND_EXECUTION)
            assert (await client.patch(url, json={"node": str(TRIAGE), "autoApprove": "yes"})).status_code == 400
            assert (await client.patch(url, json={"node": "missing", "autoApprove": True})).status_code == 400
            response = await client.patch(url, json={"node": str(TRIAGE), "autoApprove": True})
            assert response.status_code == 200
            assert response.json()["autoApproveNodes"] == [str(TRIAGE)]
            plan = await pending(ApprovalKind.PLAN_APPROVAL)
            await runtime.decide(run.run_id, plan.approval_id, ApprovalDecision.ACCEPT)
            question = await pending(ApprovalKind.USER_INPUT)
            response = await client.patch(url, json={"node": str(TRIAGE), "autoApprove": False})
            assert response.json()["autoApproveNodes"] == []
            await runtime.decide(run.run_id, question.approval_id, ApprovalDecision.ACCEPT)
            await pending(ApprovalKind.TOOL_USE)
        await runtime.aclose()

    asyncio.run(exercise())


def test_auto_approve_preference_survives_store_reopen(tmp_path: Path) -> None:
    async def exercise() -> None:
        import sqlite3

        path = tmp_path / "graph.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, "
                "error TEXT NOT NULL DEFAULT '', ordinal INTEGER)"
            )
            connection.execute("INSERT INTO runs VALUES ('old', 'triage', '', 0)")
        store = SqliteGraphRuntimeStore(path)
        assert (await store.run(RunId("old"))).auto_approve_nodes == ()
        assert (await store.run(RunId("old"))).runner_overrides == {}
        record = RunRecord(
            RunId("one"), GRAPH, auto_approve_nodes=(TRIAGE,),
            runner_overrides={TRIAGE: "claude"},
        )
        await store.remember_run(record)
        await store.remember_run(RunRecord(RunId("two"), GRAPH))
        store.close()
        store = SqliteGraphRuntimeStore(path)
        assert await store.run(record.run_id) == record
        assert (await store.run(RunId("two"))).auto_approve_nodes == ()
        store.close()

    asyncio.run(exercise())


def test_event_log_replays_and_tails_after_store_reopens(tmp_path: Path) -> None:
    from engine.graph_runtime import RuntimeEvent

    async def scenario() -> None:
        path = tmp_path / "events.db"
        store = SqliteGraphRuntimeStore(path)
        log = EventLog(store)
        run_id = RunId("conversation")
        first = await log.append(RuntimeEvent(
            run_id, EventKind.TRANSCRIPT, {"role": "user", "text": "Remember this"},
            node_id=NodeId("implementation"),
        ))
        await log.append(RuntimeEvent(RunId("other"), EventKind.RUN_STARTED))
        store.close()
        store = SqliteGraphRuntimeStore(path)
        try:
            log = EventLog(store)
            assert log.since(run_id) == (first,)
            stream = log.stream(run_id, first.sequence)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            second = await log.append(RuntimeEvent(
                run_id, EventKind.TRANSCRIPT, {"role": "assistant", "text": "Remembered"},
            ))
            assert await asyncio.wait_for(pending, 1) == second
            assert log.since(run_id, first.sequence) == (second,)
            await stream.aclose()
        finally:
            store.close()

    asyncio.run(scenario())
