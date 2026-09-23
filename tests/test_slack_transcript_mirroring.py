"""The UI transcript is the sole source of Slack agent progress/reports."""

import asyncio
import json
from contextlib import asynccontextmanager
from html import escape

import pytest
from langgraph.graph import END, START, StateGraph
from starlette.testclient import TestClient

from engine.domain import RunPhase
from engine.graph_runtime import EventKind, RunStatus
from engine.graph_runtime_langgraph import State, graph_workflow
from engine.graph_runtime_langgraph.executions import current_execution
from engine.graph_runtime_langgraph.workflows import sqlite_runtime
from engine.runtime import WorkflowCatalog, WorkOrdersConfig
from test_slack_work_orders import (
    FakeACPProvider, RecordingCommunications, _app, _signed,
)


@pytest.mark.parametrize("ending", ["finished", "failed"])
@pytest.mark.parametrize("origin", ["slack", "web"])
@pytest.mark.parametrize("fail_post", [False, True])
@pytest.mark.parametrize("before_row", [False, True])
def test_agent_transcript_is_mirrored(tmp_path, origin, fail_post, before_row, ending):
    texts = ["Inspecting the code.", "Checking <@UOTHER> & [REDACTED].", "Final report: done."]

    async def agent(state):
        execution = current_execution()
        await execution.say("hidden system prompt", role="system")
        await execution.say("user instructions", role="user")
        await execution.say(texts[0])
        await execution.tool("shell-1", "terminal", {"command": "private command"}, "private output")
        await execution.say(texts[1])
        await execution.say(texts[2])
        if ending == "failed":
            raise RuntimeError("unexpected service failure")
        return {}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    graph = graph_workflow(builder, id="mirror-test", name="Mirror test")

    @asynccontextmanager
    async def running():
        async with sqlite_runtime((graph,), tmp_path / "graph") as runtime:
            start = runtime.start

            async def start_and_wait(*args, **kwargs):
                snapshot = await start(*args, **kwargs)
                async with asyncio.timeout(10):
                    while (await runtime.snapshot(snapshot.run_id)).status is RunStatus.RUNNING:
                        await asyncio.sleep(0.01)
                return await runtime.snapshot(snapshot.run_id)

            if before_row:
                runtime.start = start_and_wait
            yield runtime

    class Communications(RecordingCommunications):
        async def post(self, channel, message, run_id=None, thread_id=""):
            if fail_post and message.text == texts[0]:
                raise RuntimeError("secret-token private request body")
            return await super().post(channel, message, run_id, thread_id)

    communications = Communications()
    app, capabilities, _ = _app(
        tmp_path, communications,
        WorkOrdersConfig(repository="acme/api", workflow="mirror-test"),
        WorkflowCatalog.from_graphs((graph,)), provider=FakeACPProvider(create=True),
        graph_runtime=running(),
    )
    with TestClient(app) as client:
        if origin == "slack":
            body = json.dumps({"type": "event_callback", "event": {
                "type": "app_mention", "channel": "CSOURCE", "user": "UREQUESTER",
                "ts": "2", "thread_ts": "1", "text": "<@BOT> new workorder please",
            }}).encode()
            assert client.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
            client.portal.call(app.state.slack_ingress.drain)
        else:
            response = client.post("/api/runs", json={
                "workflowId": "mirror-test", "prompt": "Implement it", "repository": "acme/api",
            })
            assert response.status_code < 300, response.text
        runs = client.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        run_id = runs[0].run_id

        async def finished():
            async with asyncio.timeout(10):
                while True:
                    state = await capabilities.state_store.load(run_id)
                    if state.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED):
                        return state
                    await asyncio.sleep(0.01)

        state = client.portal.call(finished)
        assert state.phase is (RunPhase.SUCCEEDED if ending == "finished" else RunPhase.FAILED)
        events = client.get(f"/api/runs/{run_id}/graph-events").json()["events"]
        diagnostics = [e for e in events if e["type"] == EventKind.NOTIFICATION_FAILED.value]
        assert len(diagnostics) == int(origin == "slack" and fail_post)
        assert "secret-token" not in str(events)
        assert "private request body" not in str(events)
        if diagnostics:
            assert diagnostics[0]["payload"] == {
                "error": "Slack notification could not be delivered.", "eventKind": "transcript",
            }
        visible = [e["payload"]["text"] for e in events
                   if e["type"] == "transcript" and e["payload"].get("role") == "assistant"]
        assert visible == texts

    if origin == "web":
        assert communications.posts == []
        return
    assert (state.origin.channel, state.origin.thread_id, state.origin.author) == (
        "CSOURCE", "1", "UREQUESTER",
    )
    posted = [m.text for _, m, _ in communications.posts]
    expected = [escape(text, quote=False) for text in texts[1 if fail_post else 0:]]
    assert [text for text in posted if text in expected] == expected
    assert "Work order finished." not in posted
    assert posted.count("Work order failed: unexpected service failure") == int(ending == "failed")
    assert posted.count("*agent* started.") == 1
    assert any("Started a work order" in text for text in posted)
    assert not any(word in text for text in posted for word in (
        "private command", "private output", "hidden system prompt", "user instructions",
    ))
    assert all((channel, thread) == ("CSOURCE", "1") for channel, _, thread in communications.posts)
