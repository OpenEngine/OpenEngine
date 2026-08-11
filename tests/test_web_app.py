"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
import json
from collections.abc import Sequence

import httpx

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import ThreadService, create_app
from engine.apps.web.composition import Settings, build_capabilities
from engine.domain import AgentId, AgentProfile, AgentRunId, Message, Role, ToolCall
from engine.ports import AgentTurn, Workspace
from engine.runtime import AgentSession, Capabilities

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Be terse.",
        description="Reads code.",
    )
}


def test_web_composes_the_sqlite_conversation_store(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"

    capabilities = build_capabilities(Settings(sqlite_path=str(database)))

    assert isinstance(capabilities.state_store, SQLiteStateStore)
    assert database.exists()
    capabilities.state_store.close()


def test_web_restores_sqlite_conversations_after_restart(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"
    runner = ConcurrentRunner(("Persisted title", "persisted answer"))

    first_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    first_app = create_app(
        AgentSession(first_capabilities, profiles=PROFILES, runners={"test": runner}),
        {"test": runner},
    )

    async def first_process() -> str:
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/title",
                json={"text": "remember this", "runner": "test"},
            )
            await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "remember this"}
            )
            await client.post(f"/api/threads/{thread_id}/archive")
            return thread_id

    thread_id = asyncio.run(first_process())
    first_capabilities.state_store.close()

    second_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    second_app = create_app(
        AgentSession(second_capabilities, profiles=PROFILES, runners={"test": runner}),
        {"test": runner},
    )

    async def second_process():
        transport = httpx.ASGITransport(app=second_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            threads = await client.get("/api/threads")
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return threads, messages

    try:
        threads, messages = asyncio.run(second_process())
    finally:
        second_capabilities.state_store.close()

    assert threads.json()["threads"] == [
        {
            "id": thread_id,
            "title": "Persisted title",
            "archived": True,
            "agentId": "coder",
            "runner": "test",
        }
    ]
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [("user", "remember this"), ("assistant", "persisted answer")]


class ConcurrentRunner:
    """A controllably slow runner that records how much work overlaps."""

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[Message, ...]] = []
        self.workspace_ids: list[str | None] = []
        self.active = 0
        self.most_active = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        reply = self.replies.pop(0) if self.replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


def _session(runner: ConcurrentRunner) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=unused,
            state_store=InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners={"test": runner},
    )


class ConversationWorkspaces:
    def __init__(self) -> None:
        self.count = 0

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        self.count += 1
        workspace_id = f"ws-{self.count}"
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/worktrees/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
        )

    async def root_path(self, workspace_id: str) -> str:
        return f"/worktrees/{workspace_id}"

    async def dispose(self, workspace_id: str) -> None:
        pass


def test_each_new_chat_reports_its_own_worktree() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    unused = object()
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces,
            state_store=InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners={"test": runner},
        workspace_repository="/repository",
    )
    app = create_app(session, {"test": runner})

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"agentId": "coder", "runner": "test"}
            first = await client.post("/api/threads", json=body)
            second = await client.post("/api/threads", json=body)
            await client.post(
                f"/api/threads/{first.json()['id']}/runs", json={"text": "inspect"}
            )
            return first.json(), second.json()

    first, second = asyncio.run(scenario())

    assert first["workspaceRoot"] == "/worktrees/ws-1"
    assert second["workspaceRoot"] == "/worktrees/ws-2"
    assert first["workspaceRoot"] != second["workspaceRoot"]
    assert runner.workspace_ids == ["ws-1"]


def test_different_chats_can_run_at_the_same_time() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> None:
        first = await service.create(CODER, "test")
        second = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(first.instance_id, "first", None, asyncio.Queue()),
            service.say(second.instance_id, "second", None, asyncio.Queue()),
        )

    asyncio.run(scenario())

    assert runner.most_active == 2


def test_one_chat_serializes_its_own_turns() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> tuple[Message, ...]:
        thread = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(thread.instance_id, "first", None, asyncio.Queue()),
            service.say(thread.instance_id, "second", None, asyncio.Queue()),
        )
        return await service.history(thread.instance_id)

    history = asyncio.run(scenario())

    assert runner.most_active == 1
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "first"),
        (Role.ASSISTANT, "one"),
        (Role.USER, "second"),
        (Role.ASSISTANT, "two"),
    ]


def test_http_api_creates_lists_and_streams_threads() -> None:
    runner = ConcurrentRunner(("hello",))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            config = await client.get("/api/config")
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "hi", "runner": "test"},
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
        return config, created, streamed, messages

    config, created, streamed, messages = asyncio.run(scenario())

    assert config.status_code == 200
    assert config.json()["defaultRunner"] == "test"
    assert created.status_code == 201
    assert streamed.status_code == 200
    assert '"type":"done"' in streamed.text
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_agent_names_chat_before_answer_without_changing_conversation() -> None:
    runner = ConcurrentRunner(('"SQLite Conversation Persistence"', "The answer."))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title",
                json={
                    "text": "Why are chats missing after restart?",
                    "runner": "test",
                },
            )
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "Why are chats missing after restart?"},
            )
            repeated_title = await client.post(
                f"/api/threads/{thread_id}/title", json={}
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return title, repeated_title, messages

    title, repeated_title, messages = asyncio.run(scenario())

    assert title.json() == {"title": "SQLite Conversation Persistence"}
    assert repeated_title.json() == title.json()
    assert runner.seen[0] == (
        Message.user("Why are chats missing after restart?"),
        Message.user(
            "Name this chat based on the conversation above. Reply with only a concise "
            "title of at most eight words, with no quotes or ending punctuation."
        ),
    )
    assert runner.seen[1] == (Message.user("Why are chats missing after restart?"),)
    assert len(runner.seen) == 2
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "Why are chats missing after restart?"),
        ("assistant", "The answer."),
    ]


def test_missing_frontend_has_an_actionable_response() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert "npm --prefix apps/web run build" in response.text


def test_tool_activity_round_trips_as_assistant_ui_parts() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class ToolRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ToolRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "inspect"})
            return (await client.get(f"/api/threads/{thread_id}/messages")).json()

    content = asyncio.run(scenario())["messages"][1]["content"]

    assert content == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
            "result": "engine",
        },
        {"type": "text", "text": "Found it."},
    ]


def test_active_run_survives_stream_disconnect_and_replays_progress() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class RefreshRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            tool_call = Message.assistant(tool_calls=(call,))
            tool_result = Message.tool_result(call.call_id, "engine")
            answer = Message.assistant("Found it.")
            on_message(tool_call)
            self.started.set()
            await self.release.wait()
            on_message(tool_result)
            on_message(answer)
            return AgentTurn(answer, steps=(tool_call, tool_result))

    runner = RefreshRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "inspect", None)
        await runner.started.wait()

        original_stream = run.stream()
        first = json.loads((await anext(original_stream)).decode())
        await original_stream.aclose()  # the browser refreshed

        assert service.active_run(thread.instance_id) is run
        active = service.active_run(thread.instance_id)
        assert active is not None
        resumed_stream = active.stream()
        replayed = json.loads((await anext(resumed_stream)).decode())

        runner.release.set()
        events = [replayed]
        async for event in resumed_stream:
            events.append(json.loads(event.decode()))
        return first, events, await service.history(thread.instance_id)

    first, events, history = asyncio.run(scenario())

    assert first["content"] == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
        }
    ]
    assert events[0] == first
    assert events[-1]["type"] == "done"
    assert events[-1]["content"][-1] == {"type": "text", "text": "Found it."}
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "inspect"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "engine"),
        (Role.ASSISTANT, "Found it."),
    ]


def test_two_observers_receive_the_same_run_state_transitions() -> None:
    class WindowRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            self.started.set()
            await self.release.wait()
            return AgentTurn(Message.assistant("shared answer"))

    runner = WindowRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        first_window = service.events(thread.instance_id)
        second_window = service.events(thread.instance_id)
        initial = await asyncio.gather(anext(first_window), anext(second_window))

        run = await service.start_run(thread.instance_id, "shared question", None)
        await runner.started.wait()
        running = await asyncio.gather(anext(first_window), anext(second_window))

        runner.release.set()
        async for _event in run.stream():
            pass
        completed = await asyncio.gather(anext(first_window), anext(second_window))

        await first_window.aclose()
        await second_window.aclose()
        return initial, running, completed

    initial, running, completed = asyncio.run(scenario())

    assert [snapshot.version for snapshot in initial] == [0, 0]
    assert [snapshot.version for snapshot in running] == [1, 1]
    assert all(snapshot.run_status == "running" for snapshot in running)
    assert all(snapshot.resumable for snapshot in running)
    assert all(snapshot.history[-1].role is Role.USER for snapshot in running)
    assert completed[0].version == completed[1].version
    assert completed[0].version > running[0].version
    assert all(snapshot.run_status == "idle" for snapshot in completed)
    assert all(not snapshot.resumable for snapshot in completed)
    assert all(snapshot.history[-1].content == "shared answer" for snapshot in completed)


def test_snapshot_retries_when_a_run_finishes_during_history_read() -> None:
    class RacingRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            await self.release.wait()
            return AgentTurn(Message.assistant("finished during the read"))

    runner = RacingRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "race", None)
        original_history = service.session.history
        stale_history_read = asyncio.Event()
        return_stale_history = asyncio.Event()
        blocked = False

        async def racing_history(instance_id):
            nonlocal blocked
            history = await original_history(instance_id)
            if not blocked and history[-1].role is Role.USER:
                blocked = True
                stale_history_read.set()
                await return_stale_history.wait()
            return history

        service.session.history = racing_history
        snapshot_task = asyncio.create_task(service.snapshot(thread.instance_id))
        await stale_history_read.wait()

        runner.release.set()
        while not run.done:
            await asyncio.sleep(0)
        return_stale_history.set()
        return await snapshot_task

    snapshot = asyncio.run(scenario())

    assert snapshot.run_status == "idle"
    assert not snapshot.resumable
    assert snapshot.history[-1].content == "finished during the read"
