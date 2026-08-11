"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
from collections.abc import Sequence

import httpx

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import ThreadService, create_app
from engine.apps.web.composition import Settings, build_capabilities
from engine.domain import AgentId, AgentProfile, AgentRunId, Message, Role, ToolCall
from engine.ports import AgentTurn
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
    runner = ConcurrentRunner(("persisted answer",))

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
                f"/api/threads/{thread_id}/runs", json={"text": "remember this"}
            )
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
            "title": "New chat",
            "archived": False,
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


def test_different_chats_can_run_at_the_same_time() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner))

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
    service = ThreadService(_session(runner))

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
