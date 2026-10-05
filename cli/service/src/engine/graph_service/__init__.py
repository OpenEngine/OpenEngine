"""Registered graphs, runs, loops and node steering, served by the engine daemon.

What `engine graph`, `engine loop` and `engine node` talk to. The daemon owns
the one LangGraph runtime; this package registers graphs on it, starts runs of
them through the daemon's own WorkOrder path, schedules loops, and steers node
executions -- every agent node a langgraph-acp session.
"""

from engine.graph_service.api import API_VERSION, create_app
from engine.graph_service.language import GraphError, GraphSpec, parse_graph
from engine.graph_service.service import (
    Conflict,
    GraphService,
    NotFound,
    ServiceError,
    StartRequest,
    StartRun,
    auth_required,
)

__all__ = [
    "API_VERSION",
    "Conflict",
    "GraphService",
    "GraphError",
    "GraphSpec",
    "NotFound",
    "ServiceError",
    "StartRequest",
    "StartRun",
    "auth_required",
    "create_app",
    "parse_graph",
]
