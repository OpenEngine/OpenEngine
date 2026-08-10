"""The planner surface: HTTP API, SSE stream, and UI.

The package a consumer embeds. It depends on `engine.runtime` and `engine.ports`
and names no adapter, so the agent backend is whatever the caller passes to
`PlannerService`. Boundary tests enforce that -- see `tests/test_boundaries.py`.

    from engine.web import PlannerService, create_app

    service = PlannerService(my_runner, workspace_root=Path("./work"))
    app = create_app(service)          # a FastAPI app; serve it however you like
"""

from engine.web.app import (
    STATIC_ROOT,
    MessageIn,
    create_app,
    event_to_json,
    plan_to_json,
    sse_frame,
    sse_stream,
)
from engine.web.service import PlannerService

__all__ = [
    "STATIC_ROOT",
    "MessageIn",
    "PlannerService",
    "create_app",
    "event_to_json",
    "plan_to_json",
    "sse_frame",
    "sse_stream",
]
