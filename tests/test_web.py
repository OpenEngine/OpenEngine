"""The planner surface over HTTP.

The point of these tests is as much architectural as functional: `engine.web` is
constructed here with a runner the web package has never heard of, which is the
property that lets a consumer bring their own backend.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.adapters.scripted import DEMO_SCRIPT, ScriptedAgentRunner
from engine.web import (
    PlannerService,
    create_app,
    event_to_json,
    plan_to_json,
    sse_stream,
)

pytest.importorskip("httpx")


def make_client(tmp_path: Path) -> TestClient:
    service = PlannerService(
        ScriptedAgentRunner(DEMO_SCRIPT),
        workspace_root=tmp_path,
        backend="scripted",
    )
    return TestClient(create_app(service))


def wait_for_complete(client: TestClient, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plan = client.get("/api/plan").json()
        if plan["is_complete"]:
            return plan
        time.sleep(0.05)
    raise AssertionError(f"plan never completed: {plan}")


def test_status_reports_the_backend(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        body = client.get("/api/status").json()
    assert body["backend"] == "scripted"
    assert body["busy"] is False


def test_plan_starts_empty(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        plan = client.get("/api/plan").json()
    assert plan["tasks"] == []
    assert plan["goal"] == ""


def test_posting_a_message_runs_a_turn(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        accepted = client.post("/api/message", json={"text": "Write a brief."})
        assert accepted.status_code == 200
        plan = wait_for_complete(client)

    assert [t["task_id"] for t in plan["tasks"]] == ["brief", "readme"]
    assert all(t["status"] == "done" for t in plan["tasks"])
    assert (tmp_path / "README.md").is_file()


def test_empty_message_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.post("/api/message", json={"text": "   "}).status_code == 400


def test_reset_starts_a_fresh_plan(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/api/message", json={"text": "go"})
        wait_for_complete(client)
        assert client.post("/api/reset").json() == {"reset": True}
        plan = client.get("/api/plan").json()
    assert plan["tasks"] == []


def read_frames(service: PlannerService, count: int, *, send: str | None = None) -> list[dict]:
    """Drive the SSE generator directly and stop after `count` frames.

    Not through TestClient: an event stream has no reason to end, so a blocking
    client would wait forever for a close that never comes. Driving the
    generator exercises the same code the route runs and terminates on `break`.
    """

    async def run() -> list[dict]:
        frames: list[dict] = []
        stream = sse_stream(service)
        try:
            if send is not None:
                service.start_turn(send)
            async for frame in stream:
                assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
                frames.append(json.loads(frame[len(b"data: ") : -2]))
                if len(frames) >= count:
                    break
        finally:
            await stream.aclose()
        return frames

    return asyncio.run(run())


def test_event_stream_opens_with_the_current_plan(tmp_path: Path) -> None:
    """A late subscriber must not see a blank board."""
    service = PlannerService(
        ScriptedAgentRunner(DEMO_SCRIPT), workspace_root=tmp_path, backend="scripted"
    )
    first = read_frames(service, 1)[0]
    assert first["type"] == "plan"
    assert first["plan"]["tasks"] == []


def test_event_stream_carries_planner_activity(tmp_path: Path) -> None:
    """Text, tool calls, and plan snapshots all reach the browser."""
    service = PlannerService(
        ScriptedAgentRunner(DEMO_SCRIPT), workspace_root=tmp_path, backend="scripted"
    )
    frames = read_frames(service, 12, send="Write a brief.")

    kinds = {f["type"] for f in frames}
    assert "text" in kinds, "planner prose never reached the stream"
    assert "plan" in kinds, "plan updates never reached the stream"
    # Every frame must be JSON the browser can parse.
    for frame in frames:
        json.dumps(frame)


def test_ui_assets_are_served(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert "<title>engine" in client.get("/").text
        assert client.get("/app.js").status_code == 200
        assert client.get("/style.css").status_code == 200


# --- the property that makes the surface shippable --------------------------


def test_service_accepts_any_runner(tmp_path: Path) -> None:
    """`engine.web` names no vendor; the backend is whatever it is handed."""
    from engine.ports import AgentRunner

    class MyOwnSession:
        def send(self, message):  # noqa: ANN001, ANN202
            async def nothing():
                return
                yield  # pragma: no cover - makes this an async generator

            return nothing()

        async def close(self) -> None:
            pass

    class MyOwnRunner:
        """A backend engine.web has never heard of."""

        def start(self, spec, invoke_tool):  # noqa: ANN001, ANN202
            return MyOwnSession()

    assert isinstance(MyOwnRunner(), AgentRunner)
    service = PlannerService(MyOwnRunner(), workspace_root=tmp_path, backend="mine")
    assert service.backend == "mine"
    assert create_app(service) is not None


def test_plan_serialisation_round_trips(tmp_path: Path) -> None:
    from engine.domain.ids import PlanId, TaskId
    from engine.domain.planning import Plan, PlanTask, TaskStatus

    plan = Plan(
        plan_id=PlanId("p"),
        goal="ship it",
        tasks=(
            PlanTask(task_id=TaskId("a"), title="A", status=TaskStatus.DONE, result="ok"),
            PlanTask(task_id=TaskId("b"), title="B", depends_on=(TaskId("a"),)),
        ),
    )
    body = plan_to_json(plan)
    assert json.loads(json.dumps(body)) == body  # JSON-serialisable
    assert body["counts"]["done"] == 1
    assert body["tasks"][1]["depends_on"] == ["a"]


def test_every_foreman_event_serialises() -> None:
    from engine.domain.ids import PlanId, TaskId
    from engine.domain.planning import Plan
    from engine.runtime import (
        ForemanError,
        PlannerText,
        PlannerThinking,
        PlanUpdated,
        ToolActivity,
        TurnEnded,
        WorkerText,
    )

    events = [
        PlannerText("hi"),
        PlannerThinking("pondering"),
        ToolActivity(name="add_task", arguments={"x": 1}, result="ok", finished=True),
        WorkerText(TaskId("a"), "output"),
        PlanUpdated(Plan(plan_id=PlanId("p"))),
        TurnEnded("end_turn"),
        ForemanError("boom"),
    ]
    for event in events:
        body = event_to_json(event)
        assert body["type"] != "unknown", f"{type(event).__name__} has no wire form"
        json.dumps(body)  # must not raise
