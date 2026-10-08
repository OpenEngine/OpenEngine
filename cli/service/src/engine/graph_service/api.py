"""The `/api/v1` HTTP surface (mounted there by the daemon) `engine graph`, `loop` and `node` commands speak.

    GET  /graphs                      registered graphs in a project
    POST /graphs                      register {format: yaml|python, source} (a new version if it changed)
    GET  /graphs/{ref}                one graph and its exact definition
    GET  /runs                        runs started here, newest first (?graph=&loop=&status=&limit=)
    POST /runs                        start a run; `idempotencyKey` makes retries safe
    GET  /runs/{run_id}               status, node executions, results, failure
    GET  /runs/{run_id}/nodes         a run's node executions
    POST /runs/{run_id}/approvals/{id} answer a question an agent stopped on
    POST /runs/{run_id}/cancel        stop a run
    GET  /nodes/{execution_id}        one node execution and its steering
    POST /nodes/{execution_id}/steering
    POST /loops   GET /loops   GET /loops/{ref}
    POST /loops/{ref}/pause   POST /loops/{ref}/resume
    POST /sessions                    start {agent, repository, baseRef?}: a run whose implementation node is your CLI
    GET  /sessions/{id}               starting | ready (workspace, mcp, instructions, settings) | ended | failed
    POST /sessions/{id}/end           {summary?}: the CLI exited; finish the run
    GET  /backend                     what this backend runs graphs with

`project` is a query parameter on reads and a body field on writes. JSON in
and out; a refusal is `{"error": ..., ...details}` with 400 for a bad request,
404 for something missing and 409 for a request that conflicts with state.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from engine.graph_service.language import GraphError
from engine.graph_service.service import DEFAULT_RUN_LIMIT, GraphService, ServiceError

API_VERSION = 1


def create_app(service: GraphService) -> Starlette:
    async def backend(_request: Request) -> JSONResponse:
        return JSONResponse({
            "apiVersion": API_VERSION,
            "runners": list(service.runners()),
            "execution": "langgraph-acp",
        })

    async def list_graphs(request: Request) -> JSONResponse:
        return JSONResponse({"graphs": service.list_graphs(request.query_params.get("project"))})

    async def add_graph(request: Request) -> JSONResponse:
        body = await _body(request)
        graph, created = await service.add_graph(
            _text(body, "project"),
            source=_text(body, "source"),
            format=_text(body, "format") or "yaml",
            name=_text(body, "name"),
        )
        return JSONResponse({**graph, "created": created}, status_code=201 if created else 200)

    async def get_graph(request: Request) -> JSONResponse:
        return JSONResponse(
            service.get_graph(request.query_params.get("project"), request.path_params["ref"])
        )

    async def list_runs(request: Request) -> JSONResponse:
        query = request.query_params
        return JSONResponse({"runs": await service.list_runs(
            query.get("project"),
            graph=query.get("graph", ""),
            loop=query.get("loop", ""),
            status=query.get("status", ""),
            limit=_count(query.get("limit"), "limit", DEFAULT_RUN_LIMIT),
        )})

    async def submit_run(request: Request) -> JSONResponse:
        body = await _body(request)
        run, created = await service.submit_run(
            project=_text(body, "project") or None,
            graph=_text(body, "graph"),
            instruction=_text(body, "instruction"),
            inputs=_object(body, "inputs"),
            repository=_text(body, "repository"),
            idempotency_key=_text(body, "idempotencyKey") or request.headers.get("Idempotency-Key", ""),
        )
        return JSONResponse({**run, "created": created}, status_code=201 if created else 200)

    async def get_run(request: Request) -> JSONResponse:
        return JSONResponse(await service.run_json(request.path_params["run_id"]))

    async def decide(request: Request) -> JSONResponse:
        body = await _body(request)
        return JSONResponse(await service.decide(
            request.path_params["run_id"], request.path_params["approval_id"], _text(body, "decision"),
        ))

    async def cancel(request: Request) -> JSONResponse:
        return JSONResponse(await service.cancel(request.path_params["run_id"]))

    async def list_nodes(request: Request) -> JSONResponse:
        return JSONResponse({"nodes": await service.list_nodes(request.path_params["run_id"])})

    async def get_node(request: Request) -> JSONResponse:
        return JSONResponse(await service.get_node(request.path_params["execution_id"]))

    async def steer(request: Request) -> JSONResponse:
        body = await _body(request)
        steering, created = await service.steer(
            request.path_params["execution_id"],
            _text(body, "message"),
            _text(body, "idempotencyKey") or request.headers.get("Idempotency-Key", ""),
        )
        return JSONResponse({**steering, "created": created}, status_code=202 if created else 200)

    async def add_loop(request: Request) -> JSONResponse:
        body = await _body(request)
        loop = await service.add_loop(
            project=_text(body, "project") or None,
            graph=_text(body, "graph"),
            name=_text(body, "name"),
            instruction=_text(body, "instruction"),
            every=body.get("every"),
            max_prs=body.get("maxPrs"),
            max_spend_usd=body.get("maxSpendUsd"),
            repository=_text(body, "repository"),
            inputs=_object(body, "inputs"),
            start_now=body.get("startNow", True) is not False,
        )
        return JSONResponse(loop, status_code=201)

    async def list_loops(request: Request) -> JSONResponse:
        return JSONResponse({"loops": await service.list_loops(request.query_params.get("project"))})

    async def get_loop(request: Request) -> JSONResponse:
        row = service.resolve_loop(request.query_params.get("project"), request.path_params["ref"])
        return JSONResponse(await service.loop_json(row.loop_id))

    async def pause_loop(request: Request) -> JSONResponse:
        body = await _body(request)
        return JSONResponse(await service.pause_loop(
            _text(body, "project") or None, request.path_params["ref"], _text(body, "reason"),
        ))

    async def resume_loop(request: Request) -> JSONResponse:
        body = await _body(request)
        return JSONResponse(await service.resume_loop(
            _text(body, "project") or None,
            request.path_params["ref"],
            max_prs=body.get("maxPrs"),
            max_spend_usd=body.get("maxSpendUsd"),
        ))

    async def start_session(request: Request) -> JSONResponse:
        body = await _body(request)
        return JSONResponse(await service.start_session(
            agent=_text(body, "agent") or "claude",
            repository=_text(body, "repository"),
            base_ref=_text(body, "baseRef"),
        ), status_code=201)

    async def get_session(request: Request) -> JSONResponse:
        return JSONResponse(service.session_json(request.path_params["session_id"]))

    async def end_session(request: Request) -> JSONResponse:
        body = await _body(request)
        return JSONResponse(await service.end_session(request.path_params["session_id"], _text(body, "summary")))

    app = Starlette(
        routes=[
            Route("/backend", _guard(backend)),
            Route("/sessions", _guard(start_session), methods=["POST"]),
            Route("/sessions/{session_id}", _guard(get_session)),
            Route("/sessions/{session_id}/end", _guard(end_session), methods=["POST"]),
            Route("/graphs", _guard(list_graphs)),
            Route("/graphs", _guard(add_graph), methods=["POST"]),
            Route("/graphs/{ref:path}", _guard(get_graph)),
            Route("/runs", _guard(list_runs)),
            Route("/runs", _guard(submit_run), methods=["POST"]),
            Route("/runs/{run_id}", _guard(get_run)),
            Route("/runs/{run_id}/nodes", _guard(list_nodes)),
            Route("/runs/{run_id}/approvals/{approval_id}", _guard(decide), methods=["POST"]),
            Route("/runs/{run_id}/cancel", _guard(cancel), methods=["POST"]),
            Route("/nodes/{execution_id}", _guard(get_node)),
            Route("/nodes/{execution_id}/steering", _guard(steer), methods=["POST"]),
            Route("/loops", _guard(list_loops)),
            Route("/loops", _guard(add_loop), methods=["POST"]),
            Route("/loops/{ref}", _guard(get_loop)),
            Route("/loops/{ref}/pause", _guard(pause_loop), methods=["POST"]),
            Route("/loops/{ref}/resume", _guard(resume_loop), methods=["POST"]),
        ],
    )
    app.state.service = service
    return app


def _guard(handler: Any) -> Any:
    async def guarded(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except GraphError as invalid:
            return JSONResponse(
                {
                    "error": "the graph is invalid",
                    "problems": [problem.json() for problem in invalid.problems],
                },
                status_code=400,
            )
        except ServiceError as refused:
            return JSONResponse({"error": str(refused), **refused.details}, status_code=refused.status)
        except _BadRequest as bad:
            return JSONResponse({"error": str(bad)}, status_code=400)

    return guarded


class _BadRequest(ValueError):
    pass


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise _BadRequest("the request body must be JSON") from None
    if not isinstance(body, dict):
        raise _BadRequest("the request body must be a JSON object")
    return body


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _BadRequest(f"{key} must be a string")
    return value


def _count(value: str | None, key: str, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise _BadRequest(f"{key} must be a whole number") from None


def _object(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key) or {}
    if not isinstance(value, dict):
        raise _BadRequest(f"{key} must be an object")
    return value


__all__ = ["API_VERSION", "create_app"]
