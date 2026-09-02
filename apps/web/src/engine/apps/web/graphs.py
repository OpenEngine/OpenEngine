"""Serve the graph control surface beside the interface.

All that is left here once the graph itself lives in a workflow file and the
runtime's files live in `engine.graph_runtime_langgraph.workflows`: mounting.
This is the app layer, so Starlette belongs in it -- and the reason it is a
wrapper rather than an edit to `engine.apps.web.api` is that the two surfaces
are two servers with the same vocabulary. Both spell a run `/api/runs`, and one
endpoint deciding which product it was answering for would be worse than two
URLs:

    /api/runs            the interface's WorkOrders
    /graph/api/runs      runs of a graph

Whether any of this is composed is not a setting. It follows from the workflows
the repository provided: a catalog holding graph workflows gets a graph runtime,
one holding only step workflows does not, and a test runs a variant by being
pointed at a different workflow directory. See `engine.runtime.workflows`.

The mount is deferred because the ordering is fixed and unfortunate: routes have
to exist before the server accepts anything, and the runtime behind them cannot
exist until the lifespan can open a checkpointer. A request that somehow beats
startup is told the server is not ready rather than handed a half-built runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from engine.graph_runtime import GraphWorkflow
from engine.graph_runtime import create_app as create_graph_app
from engine.graph_runtime_langgraph import GraphWorkflow as LangGraphWorkflow
from engine.graph_runtime_langgraph import sqlite_runtime
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

#: Where the control surface is mounted.
PREFIX = "/graph"

#: Where its files go, relative to the conversation store's directory. Beside
#: rather than inside: LangGraph's checkpoints and the runtime's own records are
#: not conversations and do not belong in the interface's database.
STATE_DIRECTORY = "graph-runtime"


class _Deferred:
    """The control surface, once the lifespan has been able to build it."""

    def __init__(self) -> None:
        self.app: Any | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.app is None:
            await JSONResponse(
                {"error": "the graph runtime is still starting"}, status_code=503
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def serve(
    web_app: Starlette, workflows: Sequence[GraphWorkflow], directory: str | Path
) -> Starlette:
    """The interface, with the graph control surface beside it under `/graph`.

    Both lifespans run: the one that opens and closes the graph runtime's two
    files, and the interface's own -- which is not otherwise driven, because a
    mounted application's lifespan is not the parent's responsibility unless the
    parent takes it on. Forgetting that would silently stop the interface from
    restoring its in-flight steps at startup.
    """
    deferred = _Deferred()
    compiled = [workflow for workflow in workflows if _compilable(workflow)]
    if len(compiled) != len(workflows):
        raise TypeError(
            "graph workflows must be written against the LangGraph binding: "
            "engine.graph_runtime_langgraph.graph_workflow"
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with sqlite_runtime(compiled, directory) as runtime:
            deferred.app = create_graph_app(runtime)
            try:
                async with web_app.router.lifespan_context(web_app):
                    yield
            finally:
                deferred.app = None

    return Starlette(
        lifespan=lifespan,
        routes=[Mount(PREFIX, deferred), Mount("/", app=web_app)],
    )


def _compilable(workflow: GraphWorkflow) -> bool:
    """Whether this graph is one *this* binding can run.

    The catalog's contract is an id and a name, which is all a loader can know.
    A deployment composing a second binding one day would check for its own here
    rather than assume; refusing early with a sentence beats a `AttributeError`
    from inside a lifespan.
    """
    return isinstance(workflow, LangGraphWorkflow)


def state_directory(sqlite_path: str) -> Path:
    """Where the graph's files go: beside the conversation store."""
    return Path(sqlite_path).resolve().parent / STATE_DIRECTORY


__all__ = ["PREFIX", "STATE_DIRECTORY", "serve", "state_directory"]
