"""HTTP surface for driving a graph.

Six things a client can do, and they are the whole reason this package exists:
start a run, read its current state, describe the graph it is running, subscribe
to what it raises, steer a node that is already running, and move control to
another node by hand.

The shape follows `engine.apps.web.api` deliberately -- JSON under `/api`,
snapshots rather than deltas, server-sent events for the feed, and refusals that
say which of "you asked for something that does not exist" and "you asked at a
moment when it could not be done" happened. A second control surface that
invented its own conventions would make the two impossible to read together.

Events are server-sent with an `id`, so a browser reconnecting sends
`Last-Event-ID` and is replayed from there without being told to poll.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from engine.domain import ApprovalDecision, ApprovalId, RunId
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from engine.langgraph_runtime.control import (
    ApprovalNotPendingError,
    GraphRuntime,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    UnknownApprovalError,
    UnknownGraphError,
    UnknownNodeError,
    UnknownRunError,
)
from engine.langgraph_runtime.events import EventLog, RuntimeEvent
from engine.langgraph_runtime.topology import GraphId, GraphTopology, NodeId

#: What each refusal means over HTTP. A missing thing is a 404, a malformed
#: request is a 400, and a request that was well formed but arrived at the wrong
#: moment is a 409 -- the client may retry that one, and only that one.
_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (UnknownGraphError, 404),
    (UnknownRunError, 404),
    (UnknownApprovalError, 404),
    (UnknownNodeError, 400),
    (ApprovalNotPendingError, 409),
    (RunNotSteerableError, 409),
)


def create_app(runtime: GraphRuntime, event_log: EventLog | None = None) -> Starlette:
    """Build the control surface around an already-composed graph runtime."""
    log = event_log if event_log is not None else EventLog()
    # Installed here rather than by the caller: the feed only replays what it
    # was told about, and a runtime whose observer was never wired would answer
    # every subscription with silence and no error to explain it.
    runtime.observe(log.append)

    async def list_graphs(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"graphs": [_topology_json(graph) for graph in runtime.graphs()]}
        )

    async def describe_graph(request: Request) -> JSONResponse:
        graph = runtime.topology(GraphId(request.path_params["graph_id"]))
        if graph is None:
            return _error("graph not found", 404)
        return JSONResponse(_topology_json(graph))

    async def start_run(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            graph_id = GraphId(_required_string(body, "graphId"))
            values = _optional_object(body, "values")
        except ValueError as error:
            return _error(str(error), 400)
        try:
            run = await runtime.start(graph_id, values)
        except Exception as error:
            return _refusal(error)
        return JSONResponse(_snapshot_json(run), status_code=201)

    async def get_run(request: Request) -> JSONResponse:
        run = await runtime.snapshot(_run_id(request))
        if run is None:
            return _error("run not found", 404)
        return JSONResponse(_snapshot_json(run))

    async def run_events(request: Request) -> Response:
        run_id = _run_id(request)
        if await runtime.snapshot(run_id) is None:
            return _error("run not found", 404)
        try:
            cursor = _cursor(request)
        except ValueError as error:
            return _error(str(error), 400)
        return StreamingResponse(
            _event_stream(log, run_id, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def steer_run(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            message = _required_string(body, "message")
        except ValueError as error:
            return _error(str(error), 400)
        try:
            run = await runtime.steer(_run_id(request), message)
        except Exception as error:
            return _refusal(error)
        return JSONResponse(_snapshot_json(run))

    async def transition_run(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            node_id = NodeId(_required_string(body, "node"))
        except ValueError as error:
            return _error(str(error), 400)
        try:
            run = await runtime.transition(_run_id(request), node_id)
        except Exception as error:
            return _refusal(error)
        return JSONResponse(_snapshot_json(run))

    async def decide_approval(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            decision = ApprovalDecision(_required_string(body, "decision"))
        except ValueError:
            allowed = ", ".join(sorted(choice.value for choice in ApprovalDecision))
            return _error(f"decision must be one of: {allowed}", 400)
        try:
            run = await runtime.decide(
                _run_id(request),
                ApprovalId(request.path_params["approval_id"]),
                decision,
            )
        except Exception as error:
            return _refusal(error)
        return JSONResponse(_snapshot_json(run))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Runs outlive the request that started them, so nothing else would stop
        # them: without this, SIGTERM drops whatever node was mid-execution.
        try:
            yield
        finally:
            await runtime.aclose()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/graphs", list_graphs),
            Route("/api/graphs/{graph_id}", describe_graph),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/runs/{run_id}/events", run_events),
            Route("/api/runs/{run_id}/steering", steer_run, methods=["POST"]),
            Route("/api/runs/{run_id}/transitions", transition_run, methods=["POST"]),
            Route(
                "/api/runs/{run_id}/approvals/{approval_id}",
                decide_approval,
                methods=["POST"],
            ),
        ]
    )
    app.state.runtime = runtime
    app.state.event_log = log
    return app


async def _event_stream(
    log: EventLog, run_id: RunId, cursor: int
) -> AsyncIterator[bytes]:
    # Flush the response before the first event, so a subscriber to a run that
    # is thinking knows it is connected. EventSource ignores comment frames.
    yield b": connected\n\n"
    async for event in log.stream(run_id, cursor):
        yield _server_event(event)


def _cursor(request: Request) -> int:
    """Where this subscriber has got to: its own answer, or the browser's.

    An explicit `cursor` wins over `Last-Event-ID`, because a client that named
    one is replaying deliberately and the header is only what the browser
    remembers.
    """
    raw = request.query_params.get("cursor")
    if raw is None:
        # Only an absent cursor falls back. An empty one is still the client
        # naming its position -- the beginning -- and honouring the browser's
        # memory instead would replay from somewhere it did not ask for.
        raw = request.headers.get("last-event-id")
    if raw is None or not raw.strip():
        return 0
    try:
        cursor = int(raw)
    except ValueError:
        raise ValueError("cursor must be an integer") from None
    if cursor < 0:
        raise ValueError("cursor must not be negative")
    return cursor


def _topology_json(graph: GraphTopology) -> dict[str, object]:
    return {
        "graphId": str(graph.graph_id),
        "name": graph.name,
        "entryPoint": str(graph.entry_point),
        "nodes": [
            {
                "nodeId": str(node.node_id),
                "name": node.name,
                "kind": node.kind,
                "description": node.description,
            }
            for node in graph.nodes
        ],
        "edges": [
            {
                "source": str(edge.source),
                "target": str(edge.target),
                "condition": edge.condition,
            }
            for edge in graph.edges
        ],
    }


def _snapshot_json(run: RunSnapshot) -> dict[str, object]:
    return {
        "runId": str(run.run_id),
        "graphId": str(run.graph_id),
        "status": run.status.value,
        "currentNode": str(run.current_node) if run.current_node else None,
        "visited": [str(node_id) for node_id in run.visited],
        "values": dict(run.values),
        "pendingApprovals": [
            _approval_json(approval) for approval in run.pending_approvals
        ],
        "error": run.error,
    }


def _approval_json(approval: PendingApproval) -> dict[str, object]:
    return {
        "approvalId": str(approval.approval_id),
        "nodeId": str(approval.node_id),
        "kind": approval.kind.value,
        "reason": approval.reason,
        "command": approval.command,
        "toolName": approval.tool_name,
        "allowedDecisions": [
            decision.value for decision in approval.allowed_decisions
        ],
    }


def _event_json(event: RuntimeEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "type": event.kind.value,
        "runId": str(event.run_id),
        "nodeId": str(event.node_id) if event.node_id else None,
        "payload": dict(event.payload),
    }


def _server_event(event: RuntimeEvent) -> bytes:
    """One SSE frame, identified so a reconnect can name where it got to."""
    body = json.dumps(_event_json(event), separators=(",", ":"))
    return f"id:{event.sequence}\ndata:{body}\n\n".encode()


def _refusal(error: Exception) -> JSONResponse:
    status = next(
        (code for kind, code in _STATUS if isinstance(error, kind)),
        None,
    )
    if status is None:
        raise error
    return _error(str(error), status)


async def _json_body(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except ValueError:
        # `ValueError` rather than `JSONDecodeError`: Starlette hands raw bytes
        # to `json.loads`, which decodes them itself, so a body that is not
        # UTF-8 raises `UnicodeDecodeError`. Both are `ValueError`, and letting
        # one of them past here is the difference between the 400 every other
        # unreadable body gets and a 500.
        return {}
    return body if isinstance(body, dict) else {}


def _required_string(body: Mapping[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_object(body: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = body.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _run_id(request: Request) -> RunId:
    return RunId(request.path_params["run_id"])


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


__all__ = ["create_app"]
