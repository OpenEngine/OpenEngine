"""HTTP surface for the planner.

The browser POSTs a message; a single Server-Sent Events stream carries
everything back -- planner text, tool activity, worker output, and a fresh plan
snapshot after every change. SSE rather than WebSockets because the traffic is
one-directional once a turn starts, and an EventSource reconnects on its own.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from engine.domain.planning import Plan
from engine.runtime import (
    ForemanError,
    ForemanEvent,
    PlannerText,
    PlannerThinking,
    PlanUpdated,
    ToolActivity,
    TurnEnded,
    WorkerText,
)
from engine.web.service import PlannerService

#: Shipped inside the package so a built wheel carries the UI with it.
STATIC_ROOT = Path(__file__).resolve().parent / "static"


class MessageIn(BaseModel):
    text: str


def plan_to_json(plan: Plan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "goal": plan.goal,
        "is_complete": plan.is_complete,
        "counts": plan.counts(),
        "tasks": [
            {
                "task_id": t.task_id,
                "title": t.title,
                "detail": t.detail,
                "status": t.status.value,
                "depends_on": list(t.depends_on),
                "result": t.result,
            }
            for t in plan.tasks
        ],
    }


def event_to_json(event: ForemanEvent) -> dict[str, Any]:
    match event:
        case PlanUpdated(plan=plan):
            return {"type": "plan", "plan": plan_to_json(plan)}
        case PlannerText(text=text):
            return {"type": "text", "text": text}
        case PlannerThinking(summary=summary):
            return {"type": "thinking", "summary": summary}
        case ToolActivity():
            return {"type": "tool", **asdict(event)}
        case WorkerText(task_id=task_id, text=text):
            return {"type": "worker", "task_id": task_id, "text": text}
        case TurnEnded(stop_reason=stop_reason):
            return {"type": "turn_ended", "stop_reason": stop_reason}
        case ForemanError(message=message):
            return {"type": "error", "message": message}
        case _:
            return {"type": "unknown"}


def sse_frame(payload: dict[str, Any]) -> bytes:
    """One Server-Sent Events frame."""
    return f"data: {json.dumps(payload)}\n\n".encode()


async def sse_stream(service: PlannerService) -> AsyncIterator[bytes]:
    """The event feed, as an async generator.

    Extracted from the route so it can be driven directly in a test. Consuming a
    few frames and breaking closes the generator through the normal `aclose`
    path -- exercising the same code the server runs, without a live connection
    that has no reason to ever end.
    """
    # Send the current plan first so a late subscriber isn't blank.
    yield sse_frame({"type": "plan", "plan": plan_to_json(service.foreman.plan)})
    async for event in service.subscribe():
        yield sse_frame(event_to_json(event))


def create_app(service: PlannerService) -> FastAPI:
    app = FastAPI(title="engine planner")

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "backend": service.backend,
            "model": service.model,
            "busy": service.busy,
            "workspace": str(service.workspace_root),
        }

    @app.get("/api/plan")
    async def plan() -> dict[str, Any]:
        return plan_to_json(service.foreman.plan)

    @app.post("/api/message")
    async def message(body: MessageIn) -> JSONResponse:
        text = body.text.strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        if not service.start_turn(text):
            return JSONResponse({"error": "planner is busy"}, status_code=409)
        return JSONResponse({"accepted": True})

    @app.post("/api/reset")
    async def reset() -> dict[str, bool]:
        await service.reset()
        return {"reset": True}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            sse_stream(service),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @app.get("/app.js")
    async def script() -> FileResponse:
        return FileResponse(STATIC_ROOT / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def styles() -> FileResponse:
        return FileResponse(STATIC_ROOT / "style.css", media_type="text/css")

    return app


__all__ = [
    "STATIC_ROOT",
    "MessageIn",
    "create_app",
    "event_to_json",
    "plan_to_json",
    "sse_frame",
    "sse_stream",
]
