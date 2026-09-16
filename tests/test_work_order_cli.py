"""The terminal client speaks only the daemon's public HTTP contract."""

import asyncio
import io
import json

import httpx
import pytest
from rich.console import Console

from engine.apps.cli import __main__ as cli


def console():
    return Console(file=io.StringIO(), width=100, color_system=None)


def test_submit_uses_work_order_api(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"runId": "run-123"})

    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(respond)))
    args = cli.parser().parse_args([
        "submit", "Fix the bug", "--repository", "org/repo", "--workflow", "review",
        "--input", "branch=topic=a", "--no-watch",
    ])
    output = console()
    assert asyncio.run(cli.run(args, output)) == 0
    assert len(requests) == 1
    assert requests[0].url.path == "/api/runs"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {
        "prompt": "Fix the bug", "repository": "org/repo", "workflowId": "review",
        "inputs": {"branch": "topic=a"},
    }
    assert "run-123" in output.file.getvalue()


@pytest.mark.parametrize("status,code", [("completed", 0), ("failed", 1)])
def test_watch_drains_final_transcript_and_returns_status(status, code):
    async def scenario():
        def respond(request):
            if request.url.path.endswith("graph-events"):
                return httpx.Response(200, json={"events": [{
                    "sequence": 1, "type": "transcript", "nodeId": "implement",
                    "payload": {"role": "assistant", "text": "Final [literal] message"},
                }]})
            return httpx.Response(200, json={
                "status": status, "activeExecutions": [], "error": "failure detail" if code else None,
            })

        output = console()
        async with httpx.AsyncClient(base_url="http://daemon", transport=httpx.MockTransport(respond)) as client:
            assert await cli.watch(client, "run-1", output) == code
        text = output.file.getvalue()
        assert "Final [literal] message" in text
        assert status in text
        if code:
            assert "failure detail" in text
    asyncio.run(scenario())


def test_sse_replay_reconnects_with_cursor_and_deduplicates():
    async def scenario():
        output = console()
        display = cli.Display(output, "run-1")
        calls = []
        done = asyncio.Event()
        event = {"sequence": 3, "type": "transcript", "nodeId": "review",
                 "payload": {"role": "assistant", "text": "Live message"}}

        def respond(request):
            calls.append(request.headers["Last-Event-ID"])
            if len(calls) == 2:
                done.set()
            return httpx.Response(200, text=": connected\r\n\r\nid:3\r\ndata: " + json.dumps(event) + "\r\n\r\n")

        async with httpx.AsyncClient(base_url="http://daemon", transport=httpx.MockTransport(respond)) as client:
            stream = asyncio.create_task(cli.stream_messages(client, "/graph/api/runs/run-1", display))
            await asyncio.wait_for(done.wait(), timeout=3)
            stream.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream
        assert calls == ["0", "3"]
        assert output.file.getvalue().count("Live message") == 1
    asyncio.run(scenario())


def test_progress_tracks_parallel_executions_and_approval():
    display = cli.Display(console(), "run-1")
    display.snapshot({"status": "running", "activeExecutions": [
        {"executionId": "a", "nodeId": "review-a"},
        {"executionId": "b", "nodeId": "review-b"},
    ]})
    assert len(display.progress.tasks) == 2
    assert all(task.total is None for task in display.progress.tasks)
    display.snapshot({"status": "awaiting_approval", "activeExecutions": [
        {"executionId": "b", "nodeId": "review-b"},
    ], "pendingApprovals": [{"approvalId": "approval"}]})
    assert len(display.progress.tasks) == 1
    assert display.progress.tasks[0].description == "review-b"
    assert display.approvals
    display.snapshot({"status": "completed", "activeExecutions": []})
    assert not display.progress.tasks


def test_http_refusal_is_readable():
    async def scenario():
        async with httpx.AsyncClient(base_url="http://daemon", transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "authentication required"})
        )) as client:
            with pytest.raises(RuntimeError, match="HTTP 401: authentication required"):
                await cli.request(client, "POST", "/api/runs")
    asyncio.run(scenario())


def test_invalid_input_does_not_submit(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("invalid input must not reach the daemon")
    monkeypatch.setattr(cli, "request", unexpected_request)
    args = cli.parser().parse_args([
        "submit", "Fix", "--repository", "org/repo", "--workflow", "review", "--input", "invalid",
    ])
    with pytest.raises(ValueError, match="NAME=VALUE"):
        asyncio.run(cli.run(args, console()))


def test_live_watch_receives_messages_before_completion_and_detaches_stream():
    async def scenario():
        output = console()
        delivered = asyncio.Event()
        closed = asyncio.Event()
        snapshots = 0
        event = {"sequence": 1, "type": "transcript", "nodeId": "implement",
                 "payload": {"role": "assistant", "text": "Working now"}}

        class Feed(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"data:{json.dumps(event)}\n\n".encode()
                delivered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def respond(request):
            nonlocal snapshots
            assert request.method == "GET"
            if request.url.path.endswith("/events"):
                return httpx.Response(200, stream=Feed())
            if request.url.path.endswith("/graph-events"):
                return httpx.Response(200, json={"events": [event]})
            snapshots += 1
            if snapshots > 1:
                await asyncio.wait_for(delivered.wait(), 1)
                assert "Working now" in output.file.getvalue()
            return httpx.Response(200, json={
                "status": "running" if snapshots == 1 else "completed",
                "activeExecutions": ([{"executionId": "a", "nodeId": "implement"}]
                                     if snapshots == 1 else []),
            })

        async with httpx.AsyncClient(base_url="http://daemon", transport=httpx.MockTransport(respond)) as client:
            assert await asyncio.wait_for(cli.watch(client, "run-1", output), 3) == 0
        assert closed.is_set()
        assert output.file.getvalue().count("Working now") == 1
    asyncio.run(scenario())
