"""The LangGraph control surface: runs, state, topology, events, steering, transitions.

Driven over HTTP against a scripted graph rather than against the runtime object,
because the HTTP surface is what this package is for -- a test that called
`ScriptedLangGraph.steer` directly would pass whatever the wire format did.

The graph under test is a two-node pipeline, implementation then review, which is
the shape the manual-transition requirement is written in: "send the run back to
implementation". See `tests/langgraph_fakes.py` for what a script is.

Requests are made with httpx, except the event feed. That one is called as ASGI
directly: httpx's ASGI transport buffers a whole response before returning it,
and a subscription that stays open so a run can be sent back to an earlier node
never lets it finish. The route is the real one either way.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from engine.langgraph_runtime import GraphId, GraphRuntime, NodeId, create_app
from langgraph_fakes import (
    Ask,
    AwaitSteering,
    Beat,
    Call,
    Fail,
    Say,
    ScriptedGraph,
    ScriptedLangGraph,
    ScriptedNode,
)

GRAPH = GraphId("implementation-review")
IMPLEMENTATION = NodeId("implementation")
REVIEW = NodeId("review")

#: How long a test waits for an event before deciding the run is stuck. Generous,
#: because it only bounds a failure -- a passing run never waits.
PATIENCE = 5.0


def _pipeline(*implementation: Beat) -> ScriptedGraph:
    """Implementation, then review, with the implementation node scripted."""
    return ScriptedGraph(
        GRAPH,
        "Implementation and review",
        (
            ScriptedNode(
                IMPLEMENTATION,
                implementation,
                next_node=REVIEW,
                name="Implementation",
            ),
            ScriptedNode(REVIEW, (Say("Looks right."),), name="Review"),
        ),
    )


class _Feed:
    """An open subscription, read one event at a time."""

    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = b""

    async def _frame(self) -> str:
        while b"\n\n" not in self._buffer:
            self._buffer += await self.chunks.get()
        frame, _, self._buffer = self._buffer.partition(b"\n\n")
        return frame.decode()

    async def event(self) -> dict:
        """The next event, skipping the comment frame that opens the stream."""
        while True:
            frame = await self._frame()
            if frame.startswith(":"):
                continue
            identifier, _, data = frame.partition("\n")
            event = json.loads(data.removeprefix("data:"))
            # The id is what a browser sends back as `Last-Event-ID`, so it has
            # to be the cursor and not some second numbering.
            assert identifier == f"id:{event['sequence']}"
            return event

    async def until(self, kind: str) -> list[dict]:
        """Everything up to and including the next event of `kind`."""
        events: list[dict] = []
        async with asyncio.timeout(PATIENCE):
            while True:
                events.append(await self.event())
                if events[-1]["type"] == kind:
                    return events


@dataclass(slots=True)
class _Surface:
    """One control server, reachable both ways the tests need it."""

    app: object
    client: httpx.AsyncClient

    def subscribe(
        self,
        run_id: str,
        *,
        cursor: int | str | None = None,
        last_event_id: int | None = None,
    ):
        return _subscribe(self.app, run_id, cursor=cursor, last_event_id=last_event_id)

    async def read(self, run_id: str, kind: str, **where: int) -> list[dict]:
        """Subscribe, collect up to `kind`, and close again."""
        async with self.subscribe(run_id, **where) as feed:
            return await feed.until(kind)


@asynccontextmanager
async def _subscribe(
    app: object, run_id: str, *, cursor: int | str | None, last_event_id: int | None
) -> AsyncIterator[_Feed]:
    feed = _Feed()
    headers = [(b"host", b"test")]
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode()))
    path = f"/api/runs/{run_id}/events"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"" if cursor is None else f"cursor={cursor}".encode(),
        "headers": headers,
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    hung_up = asyncio.Event()

    async def receive() -> dict[str, object]:
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            feed.chunks.put_nowait(message["body"])

    served = asyncio.create_task(app(scope, receive, send))  # type: ignore[operator]
    try:
        yield feed
    finally:
        hung_up.set()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


@asynccontextmanager
async def _server(runtime: ScriptedLangGraph) -> AsyncIterator[_Surface]:
    """A running control server, entered and left the way a real one is.

    Through the app's own lifespan rather than by calling `runtime.aclose()`
    here, so what stops the run tasks between test cases is the same shutdown a
    SIGTERM reaches -- a lifespan that stopped nothing would leak tasks into the
    next test instead of passing quietly.
    """
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield _Surface(app, client)


async def _start(surface: _Surface, **body: object) -> dict:
    started = await surface.client.post(
        "/api/runs", json={"graphId": str(GRAPH), **body}
    )
    assert started.status_code == 201, started.text
    return started.json()


def _kinds(events: Sequence[dict]) -> list[str]:
    return [event["type"] for event in events]


def _of_kind(events: Sequence[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def _transcript(events: Sequence[dict]) -> list[tuple[str, str]]:
    return [
        (event["payload"]["role"], event["payload"]["text"])
        for event in _of_kind(events, "transcript")
    ]


# --- the contract itself ---------------------------------------------------


def test_the_scripted_graph_satisfies_the_runtime_contract() -> None:
    """The fake is a `GraphRuntime`, so the binding that replaces it is comparable."""
    assert isinstance(ScriptedLangGraph(_pipeline(Say("Done."))), GraphRuntime)


# --- topology --------------------------------------------------------------


def test_topology_describes_every_node_and_edge() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return (
                await surface.client.get("/api/graphs"),
                await surface.client.get(f"/api/graphs/{GRAPH}"),
                await surface.client.get("/api/graphs/nonexistent"),
            )

    listed, described, missing = asyncio.run(scenario())

    assert [graph["graphId"] for graph in listed.json()["graphs"]] == [str(GRAPH)]
    assert described.status_code == 200
    assert described.json() == {
        "graphId": str(GRAPH),
        "name": "Implementation and review",
        "entryPoint": str(IMPLEMENTATION),
        "nodes": [
            {
                "nodeId": str(IMPLEMENTATION),
                "name": "Implementation",
                "kind": "agent",
                "description": "",
            },
            {
                "nodeId": str(REVIEW),
                "name": "Review",
                "kind": "agent",
                "description": "",
            },
        ],
        "edges": [
            {"source": str(IMPLEMENTATION), "target": str(REVIEW), "condition": ""}
        ],
    }
    assert missing.status_code == 404
    assert missing.json() == {"error": "graph not found"}


# --- starting runs and reading their state ---------------------------------


def test_starting_a_run_reports_the_node_it_begins_at() -> None:
    async def scenario() -> dict:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return await _start(surface, values={"repository": "acme/api"})

    run = asyncio.run(scenario())

    assert run["graphId"] == str(GRAPH)
    assert run["status"] == "running"
    assert run["currentNode"] == str(IMPLEMENTATION)
    assert run["values"] == {"repository": "acme/api"}
    assert run["pendingApprovals"] == []


def test_starting_a_graph_nobody_registered_is_refused() -> None:
    async def scenario() -> httpx.Response:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return await surface.client.post(
                "/api/runs", json={"graphId": "nonexistent"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 404
    assert refused.json() == {"error": "unknown graph: nonexistent"}


def test_current_state_names_the_node_a_paused_run_is_waiting_in() -> None:
    """The pause is the case reading state exists for: nothing else will move."""
    graph = _pipeline(
        Say("Ready to run the tests."), Ask("run the tests", command="pytest")
    )

    async def scenario() -> dict:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            await surface.read(str(run["runId"]), "approval.requested")
            paused = await surface.client.get(f"/api/runs/{run['runId']}")
            return paused.json()

    paused = asyncio.run(scenario())

    assert paused["status"] == "awaiting_approval"
    assert paused["currentNode"] == str(IMPLEMENTATION)
    assert paused["visited"] == [str(IMPLEMENTATION)]
    assert paused["values"] == {str(IMPLEMENTATION): "Ready to run the tests."}
    approval = paused["pendingApprovals"][0]
    assert approval["nodeId"] == str(IMPLEMENTATION)
    assert approval["command"] == "pytest"
    assert approval["reason"] == "run the tests"
    assert approval["kind"] == "command_execution"
    assert approval["allowedDecisions"] == ["accept", "cancel"]


def test_a_run_nobody_started_is_not_found() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return (
                await surface.client.get("/api/runs/run-404"),
                await surface.client.get("/api/runs/run-404/events"),
            )

    state, feed = asyncio.run(scenario())

    assert state.status_code == 404
    # The feed too: a subscription to a run that does not exist would otherwise
    # hang open looking like a run that has not said anything yet.
    assert feed.status_code == 404


# --- subscribing to events -------------------------------------------------


def test_the_feed_carries_transcript_events_and_tool_calls() -> None:
    graph = _pipeline(
        Say("Reading the tree."),
        Call("shell", {"command": "pytest"}, result="14 passed"),
        Say("The tests pass."),
    )

    async def scenario() -> list[dict]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            return await surface.read(str(run["runId"]), "run.finished")

    events = asyncio.run(scenario())

    assert _kinds(events) == [
        "run.started",
        "node.started",
        "transcript",
        "tool.call",
        "tool.result",
        "transcript",
        "node.finished",
        "node.started",
        "transcript",
        "node.finished",
        "run.finished",
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert _transcript(events) == [
        ("assistant", "Reading the tree."),
        ("assistant", "The tests pass."),
        ("assistant", "Looks right."),
    ]
    call, result = _of_kind(events, "tool.call")[0], _of_kind(events, "tool.result")[0]
    assert call["nodeId"] == str(IMPLEMENTATION)
    assert call["payload"]["name"] == "shell"
    assert call["payload"]["arguments"] == {"command": "pytest"}
    assert result["payload"]["callId"] == call["payload"]["callId"]
    assert result["payload"]["result"] == "14 passed"


def test_one_open_subscription_sees_the_run_it_watched_finish_and_start_again() -> None:
    """The feed outlives `run.finished`, on the feed rather than on a reconnect.

    A subscription that closed itself on the terminal event would still pass a
    suite that re-subscribes with a cursor after each transition, so this one
    holds a single feed open across the whole thing: finish, revert by hand, and
    watch the same feed carry the run starting again.
    """

    async def scenario() -> list[dict]:
        runtime = ScriptedLangGraph(_pipeline(Say("Wrote the code.")))
        async with _server(runtime) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            async with surface.subscribe(run_id) as feed:
                await feed.until("run.finished")
                reverted = await surface.client.post(
                    f"/api/runs/{run_id}/transitions",
                    json={"node": str(IMPLEMENTATION)},
                )
                assert reverted.status_code == 200, reverted.text
                return await feed.until("node.started")

    after = asyncio.run(scenario())

    assert _kinds(after) == ["transition", "node.started"]
    assert after[-1]["nodeId"] == str(IMPLEMENTATION)


def test_two_subscribers_at_different_cursors_each_see_the_whole_run() -> None:
    """Fan-out is the log's job, so more than one reader has to be real.

    Two feeds open at once on the same run, one from the beginning and one from
    partway through, is where a shared condition and a shared cursor would go
    wrong. Each sees every event it asked for, in order, and neither advances
    the other past anything.
    """
    graph = _pipeline(Say("Reading the tree."), AwaitSteering(), Say("Renamed it."))

    async def scenario() -> tuple[list[dict], list[dict]]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            async with surface.subscribe(run_id) as first:
                opening = await first.until("transcript")
                async with surface.subscribe(
                    run_id, cursor=opening[-1]["sequence"]
                ) as second:
                    await surface.client.post(
                        f"/api/runs/{run_id}/steering", json={"message": "Rename it."}
                    )
                    both = await asyncio.gather(
                        first.until("run.finished"), second.until("run.finished")
                    )
            return opening + both[0], both[1]

    whole, late = asyncio.run(scenario())

    assert _kinds(whole)[:3] == ["run.started", "node.started", "transcript"]
    assert _kinds(whole)[-1] == "run.finished"
    assert [event["sequence"] for event in whole] == list(range(1, len(whole) + 1))
    # The late reader saw everything after its cursor and nothing before it.
    assert late == whole[3:]


def test_a_subscriber_replays_from_its_own_cursor() -> None:
    """A reconnecting client asks for what it missed, not for everything."""

    async def scenario() -> tuple[list[dict], list[dict], httpx.Response]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            resumed = await surface.read(
                run_id, "run.finished", cursor=everything[2]["sequence"]
            )
            refused = await surface.client.get(
                f"/api/runs/{run_id}/events", params={"cursor": "later"}
            )
            return everything, resumed, refused

    everything, resumed, refused = asyncio.run(scenario())

    assert _kinds(resumed) == _kinds(everything)[3:]
    assert resumed[0]["sequence"] == everything[3]["sequence"]
    assert refused.status_code == 400
    assert refused.json() == {"error": "cursor must be an integer"}


def test_a_cursor_before_the_beginning_is_refused() -> None:
    async def scenario() -> httpx.Response:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            return await surface.client.get(
                f"/api/runs/{run['runId']}/events", params={"cursor": "-1"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": "cursor must not be negative"}


def test_a_browser_reconnects_with_the_event_id_it_last_saw() -> None:
    """`Last-Event-ID` is what EventSource sends; honouring it avoids a poll."""

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            async with surface.subscribe(
                run_id, last_event_id=everything[-2]["sequence"]
            ) as feed:
                async with asyncio.timeout(PATIENCE):
                    return everything, await feed.event()

    everything, first = asyncio.run(scenario())

    assert first == everything[-1]
    assert first["type"] == "run.finished"


def test_an_explicit_cursor_beats_the_header_even_when_it_is_empty() -> None:
    """An empty `cursor` is a position -- the beginning -- not an absent one.

    A client that says `?cursor=` is asking to replay the run from the start,
    and honouring the browser's memory instead would answer with somewhere it
    did not ask for.
    """

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            async with surface.subscribe(
                run_id, cursor="", last_event_id=everything[-2]["sequence"]
            ) as feed:
                async with asyncio.timeout(PATIENCE):
                    return everything, await feed.event()

    everything, first = asyncio.run(scenario())

    assert first == everything[0]
    assert first["type"] == "run.started"


# --- a node that raises ----------------------------------------------------


def test_a_node_that_raises_fails_the_run_where_it_raised() -> None:
    """The failure path, which is ordinary rather than exceptional.

    A run whose task died silently would report `running` forever, hold every
    subscriber waiting for a terminal event, and refuse steering as having no
    node in flight -- three answers a client cannot reconcile.
    """
    graph = _pipeline(Say("Running the tests."), Fail("codex is out of quota"))

    async def scenario() -> tuple[list[dict], dict, httpx.Response]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            events = await surface.read(run_id, "run.failed")
            state = await surface.client.get(f"/api/runs/{run_id}")
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering", json={"message": "try again"}
            )
            return events, state.json(), steered

    events, state, steered = asyncio.run(scenario())

    assert events[-1]["payload"] == {"error": "codex is out of quota"}
    assert state["status"] == "failed"
    assert state["error"] == "codex is out of quota"
    # Stopped in the node that raised, so a manual transition can send it back.
    assert state["currentNode"] == str(IMPLEMENTATION)
    # Review never ran, and the refusal to steer now agrees with the status.
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION)
    ]
    assert steered.status_code == 409


def test_a_failed_run_can_be_sent_back_and_run_again() -> None:
    """Recovery is the reason a failure has to name the node it happened in."""
    graph = ScriptedGraph(
        GRAPH,
        "Implementation and review",
        (
            ScriptedNode(
                IMPLEMENTATION,
                (Fail("codex is out of quota"),),
                next_node=REVIEW,
                name="Implementation",
            ),
            ScriptedNode(REVIEW, (Say("Looks right."),), name="Review"),
        ),
    )

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            failed = await surface.read(run_id, "run.failed")
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            return failed, reverted.json()

    failed, reverted = asyncio.run(scenario())

    assert failed[-1]["type"] == "run.failed"
    # The error is cleared by the transition: it describes the attempt that is
    # being replaced, and a run that carried it forward would look failed while
    # running.
    assert reverted["status"] == "running"
    assert reverted["error"] == ""
    assert reverted["currentNode"] == str(IMPLEMENTATION)


# --- shutdown --------------------------------------------------------------


def test_shutting_the_server_down_stops_the_runs_it_was_driving() -> None:
    """Runs outlive the request that started them, so nothing else would.

    Without this a SIGTERM drops whatever node was mid-execution, with no
    chance for an implementation to checkpoint it.
    """
    runtime = ScriptedLangGraph(_pipeline(Say("Reading."), AwaitSteering()))

    async def scenario() -> list[str]:
        async with _server(runtime) as surface:
            run = await _start(surface)
            await surface.read(str(run["runId"]), "transcript")
        return [str(run.run_id) for run in runtime.running()]

    assert asyncio.run(scenario()) == []


# --- approvals -------------------------------------------------------------


def test_an_approval_is_published_answered_and_answered_only_once() -> None:
    graph = _pipeline(Ask("run the tests", command="pytest", tool_name="shell"))

    async def scenario() -> dict[str, object]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested")
            approval_id = asked[-1]["payload"]["approvalId"]
            answer = f"/api/runs/{run_id}/approvals/{approval_id}"
            unknown = await surface.client.post(
                f"/api/runs/{run_id}/approvals/approval-404",
                json={"decision": "accept"},
            )
            garbled = await surface.client.post(answer, json={"decision": "maybe"})
            accepted = await surface.client.post(answer, json={"decision": "accept"})
            finished = await surface.read(run_id, "run.finished")
            again = await surface.client.post(answer, json={"decision": "accept"})
            return {
                "asked": asked,
                "unknown": unknown,
                "garbled": garbled,
                "accepted": accepted,
                "finished": finished,
                "again": again,
            }

    answered = asyncio.run(scenario())

    request = answered["asked"][-1]
    assert request["nodeId"] == str(IMPLEMENTATION)
    assert request["payload"]["command"] == "pytest"
    assert request["payload"]["toolName"] == "shell"
    assert answered["unknown"].status_code == 404
    assert answered["garbled"].status_code == 400
    assert answered["garbled"].json() == {
        "error": "decision must be one of: accept, accept_for_session, cancel"
    }
    # The reply is the run as the decision left it -- released, and no longer
    # asking -- rather than however far the node then got. A reply that waited
    # for the graph would say `running` or `completed` depending on the script.
    assert answered["accepted"].status_code == 200
    assert answered["accepted"].json()["status"] == "running"
    assert answered["accepted"].json()["pendingApprovals"] == []
    resolved = _of_kind(answered["finished"], "approval.resolved")[0]
    assert resolved["payload"] == {
        "approvalId": request["payload"]["approvalId"],
        "decision": "accept",
    }
    # The second answer is a race that lost, not a request that never existed.
    assert answered["again"].status_code == 409


def test_cancelling_an_approval_fails_the_run_where_it_asked() -> None:
    graph = _pipeline(Ask("delete the branch", command="git branch -D main"))

    async def scenario() -> tuple[list[dict], dict, dict]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested")
            refused = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{asked[-1]['payload']['approvalId']}",
                json={"decision": "cancel"},
            )
            events = await surface.read(run_id, "run.failed")
            state = await surface.client.get(f"/api/runs/{run_id}")
            return events, state.json(), refused.json()

    events, state, refused = asyncio.run(scenario())

    # The refusal is what the reply reports, without waiting to see the node
    # notice: the decision is what ended the run, not anything it did after.
    assert refused["status"] == "failed"
    assert refused["error"] == "delete the branch was not allowed"
    assert _of_kind(events, "approval.resolved")[0]["payload"]["decision"] == "cancel"
    assert state["status"] == "failed"
    assert state["error"] == "delete the branch was not allowed"
    # Stopped where it asked, so a manual transition has somewhere to go back to.
    assert state["currentNode"] == str(IMPLEMENTATION)
    assert state["visited"] == [str(IMPLEMENTATION)]
    # Review never ran: the run failed in the node that was refused.
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION)
    ]


# --- steering --------------------------------------------------------------


def test_steering_reaches_the_running_node_and_it_carries_on_from_there() -> None:
    """The whole requirement: a message mid-execution, and no restart.

    The node says one thing, stops, and is sent an instruction. What must be true
    afterwards is that it entered `implementation` exactly once and picked up at
    the beat after the interruption -- a node that had restarted would say its
    opening line twice.
    """
    graph = _pipeline(
        Say("Reading the tree."), AwaitSteering(), Say("Renamed the flag.")
    )

    async def scenario() -> tuple[dict, httpx.Response, list[dict], dict]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript")
            waiting = await surface.client.get(f"/api/runs/{run_id}")
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering", json={"message": "Rename the flag."}
            )
            events = await surface.read(run_id, "run.finished")
            final = await surface.client.get(f"/api/runs/{run_id}")
            return waiting.json(), steered, events, final.json()

    waiting, steered, events, final = asyncio.run(scenario())

    assert waiting["status"] == "running"
    assert waiting["currentNode"] == str(IMPLEMENTATION)
    assert steered.status_code == 200
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION),
        str(REVIEW),
    ]
    assert _transcript(events) == [
        ("assistant", "Reading the tree."),
        ("user", "Rename the flag."),
        ("assistant", "Renamed the flag."),
        ("assistant", "Looks right."),
    ]
    # Accepted for delivery first, delivered second: a client sees both, in that
    # order, and neither is the node starting again.
    steering = _of_kind(events, "steering.received")
    assert len(steering) == 1
    assert steering[0]["payload"] == {"message": "Rename the flag."}
    assert steering[0]["sequence"] < _of_kind(events, "transcript")[1]["sequence"]
    assert final["status"] == "completed"
    assert final["values"]["steering"] == ["Rename the flag."]


def test_steering_a_run_with_nothing_in_flight_is_refused() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            return (
                await surface.client.post(
                    f"/api/runs/{run_id}/steering", json={"message": "wait"}
                ),
                await surface.client.post(
                    f"/api/runs/{run_id}/steering", json={"message": "   "}
                ),
                await surface.client.post(
                    "/api/runs/run-404/steering", json={"message": "x"}
                ),
            )

    finished, blank, unknown = asyncio.run(scenario())

    # A race the client may retry, not a request it should fix.
    assert finished.status_code == 409
    assert finished.json() == {"error": "this run has no node in flight"}
    assert blank.status_code == 400
    assert blank.json() == {"error": "message must be a non-empty string"}
    assert unknown.status_code == 404


# --- manual transitions ----------------------------------------------------


def test_a_manual_transition_sends_the_run_back_and_rewinds_its_state() -> None:
    graph = _pipeline(Say("Wrote the code."))

    async def scenario() -> tuple[dict, dict, list[dict], dict]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            first = await surface.read(run_id, "run.finished")
            completed = await surface.client.get(f"/api/runs/{run_id}")
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            second = await surface.read(
                run_id, "run.finished", cursor=first[-1]["sequence"]
            )
            again = await surface.client.get(f"/api/runs/{run_id}")
            return completed.json(), reverted.json(), second, again.json()

    completed, reverted, second, again = asyncio.run(scenario())

    assert completed["status"] == "completed"
    assert completed["visited"] == [str(IMPLEMENTATION), str(REVIEW)]
    assert completed["values"] == {
        str(IMPLEMENTATION): "Wrote the code.",
        str(REVIEW): "Looks right.",
    }
    # The answer to the request is the rewound run: back at implementation, with
    # the state that node was entered with, and review no longer in its history.
    assert reverted["status"] == "running"
    assert reverted["currentNode"] == str(IMPLEMENTATION)
    assert reverted["visited"] == []
    assert reverted["values"] == {}
    # A finished run is not standing anywhere, so the transition comes from
    # nothing; the node it was interrupted in is reported when there was one.
    assert second[0]["payload"]["from"] is None
    assert second[0]["payload"]["to"] == str(IMPLEMENTATION)
    assert _kinds(second) == [
        "transition",
        "node.started",
        "transcript",
        "node.finished",
        "node.started",
        "transcript",
        "node.finished",
        "run.finished",
    ]
    assert again["status"] == "completed"
    assert again["visited"] == [str(IMPLEMENTATION), str(REVIEW)]


def test_a_transition_can_interrupt_a_node_that_is_still_running() -> None:
    """Reverting is not something only a finished run can be asked for."""
    graph = _pipeline(Say("Reading the tree."), AwaitSteering(), Say("Never reached."))

    async def scenario() -> tuple[dict, list[dict]]:
        async with _server(ScriptedLangGraph(graph)) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            waiting = await surface.read(run_id, "transcript")
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            restarted = await surface.read(
                run_id, "node.started", cursor=waiting[-1]["sequence"]
            )
            return reverted.json(), restarted

    reverted, restarted = asyncio.run(scenario())

    assert reverted["currentNode"] == str(IMPLEMENTATION)
    assert reverted["values"] == {}
    assert _kinds(restarted) == ["transition", "node.started"]
    assert restarted[0]["payload"]["from"] == str(IMPLEMENTATION)
    assert restarted[-1]["nodeId"] == str(IMPLEMENTATION)


def test_a_transition_to_a_node_the_graph_does_not_have_is_refused() -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            return (
                await surface.client.post(
                    f"/api/runs/{run_id}/transitions", json={"node": "deploy"}
                ),
                await surface.client.post(f"/api/runs/{run_id}/transitions", json={}),
                await surface.client.post(
                    "/api/runs/run-404/transitions", json={"node": str(IMPLEMENTATION)}
                ),
            )

    unknown_node, missing_node, unknown_run = asyncio.run(scenario())

    assert unknown_node.status_code == 400
    assert unknown_node.json() == {"error": "unknown node: deploy"}
    assert missing_node.status_code == 400
    assert missing_node.json() == {"error": "node must be a non-empty string"}
    assert unknown_run.status_code == 404


# --- request validation ----------------------------------------------------


@pytest.mark.parametrize(
    "body,message",
    [
        ({}, "graphId must be a non-empty string"),
        ({"graphId": "  "}, "graphId must be a non-empty string"),
        ({"graphId": str(GRAPH), "values": []}, "values must be an object"),
    ],
)
def test_starting_a_run_refuses_a_body_it_cannot_read(
    body: dict[str, object], message: str
) -> None:
    async def scenario() -> httpx.Response:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return await surface.client.post("/api/runs", json=body)

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": message}


@pytest.mark.parametrize(
    "body", [b"not json", b'{"graphId": "\xff"}'], ids=["unparseable", "not utf-8"]
)
def test_a_body_that_is_not_json_at_all_is_still_a_400(body: bytes) -> None:
    """Including one that is not even text.

    Starlette hands raw bytes to `json.loads`, which decodes them itself, so a
    body that is not UTF-8 raises `UnicodeDecodeError` rather than
    `JSONDecodeError` -- the one way a client could make this surface 500.
    """

    async def scenario() -> httpx.Response:
        async with _server(ScriptedLangGraph(_pipeline(Say("Done.")))) as surface:
            return await surface.client.post(
                "/api/runs",
                content=body,
                headers={"content-type": "application/json"},
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": "graphId must be a non-empty string"}
