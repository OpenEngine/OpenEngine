"""Connecting to an agent, and holding a conversation with one.

Every test here launches a real child process and speaks the real protocol to
it. The thing being tested is a process boundary -- framing, a handshake, an
interleaved stream, a shutdown that does not leave a process running -- and a
mock would exercise none of it while agreeing with whatever the code does.

`tests/fake_agent.py` is the other end. Its flags choose which agent to be: one
that cannot resume, one that asks permission, one that dies mid-request.
"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import aclosing, asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from langgraph_acp import (
    ACPAgentCapabilityError,
    ACPClient,
    ACPConnectionError,
    ACPEvent,
    ACPEventType,
    ACPSession,
    ACPSessionError,
    StdioACPProvider,
)

FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"


def asyncio_test(
    test: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., None]:
    """Run an async test. A decorator rather than a plugin, so that the package
    keeps the empty dependency list its first ticket established."""

    @wraps(test)
    def synchronously(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return synchronously


def fake_agent(*options: str, log: Path | None = None) -> StdioACPProvider:
    return StdioACPProvider(
        name="fake",
        command=(sys.executable, str(FAKE_AGENT), *options),
        env=None if log is None else {"FAKE_AGENT_LOG": str(log)},
    )


@asynccontextmanager
async def connected(*options: str, log: Path | None = None) -> AsyncIterator[ACPClient]:
    client = await fake_agent(*options, log=log).connect()
    try:
        yield client
    finally:
        await client.close()


def sent(log: Path) -> list[dict[str, Any]]:
    """Every message the agent received, so a test can assert on what was sent."""
    lines = log.read_text().splitlines() if log.exists() else []
    return [json.loads(line) for line in lines]


def methods(log: Path) -> list[str]:
    return [message["method"] for message in sent(log) if "method" in message]


def params_of(log: Path, method: str) -> dict[str, Any]:
    for message in sent(log):
        if message.get("method") == method:
            found: dict[str, Any] = message.get("params") or {}
            return found
    raise AssertionError(f"{method} was never sent; got {methods(log)}")


@asyncio_test
async def test_connecting_completes_the_handshake() -> None:
    """A client exists only once the agent has answered `initialize`."""
    async with connected() as client:
        assert client.agent == "fake"
        assert client.capabilities.protocol_version == 1


@asyncio_test
async def test_capabilities_are_the_agent_s_own_answer() -> None:
    async with connected() as client:
        capabilities = client.capabilities

        assert capabilities.load_session
        assert capabilities.prompt_image
        assert capabilities.prompt_embedded_context
        assert not capabilities.prompt_audio
        assert capabilities.mcp_http
        assert not capabilities.mcp_sse
        assert capabilities.auth_methods == ("oauth",)


@asyncio_test
async def test_an_omitted_capability_reads_as_unsupported() -> None:
    """What the protocol says an omission means, and what lets a node fail early."""
    async with connected("--no-resume") as client:
        assert not client.capabilities.load_session


@asyncio_test
async def test_a_capability_this_release_has_no_field_for_survives_in_raw() -> None:
    async with connected() as client:
        assert client.capabilities.raw["_futureCapability"] == "kept in raw"


@asyncio_test
async def test_the_client_advertises_only_what_it_can_honour(tmp_path: Path) -> None:
    """An advertised capability nobody implements is a request that hangs."""
    log = tmp_path / "sent.jsonl"
    async with connected(log=log):
        pass

    assert params_of(log, "initialize")["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


@asyncio_test
async def test_a_new_session_is_named_by_the_agent() -> None:
    async with connected() as client:
        session = await client.new_session()

        assert isinstance(session, ACPSession)
        assert session.session_id == "sess_fake_1"


@asyncio_test
async def test_a_session_is_given_an_absolute_workspace(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    async with connected(log=log) as client:
        await client.new_session(cwd=tmp_path)

    assert params_of(log, "session/new")["cwd"] == str(tmp_path)


@asyncio_test
async def test_a_session_without_a_workspace_gets_this_process_s(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    async with connected(log=log) as client:
        await client.new_session()

    assert params_of(log, "session/new")["cwd"] == str(Path.cwd())


@asyncio_test
async def test_mcp_servers_reach_the_agent_as_given(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    server = {"name": "github", "command": "github-mcp-server", "args": ["--stdio"]}
    async with connected(log=log) as client:
        await client.new_session(mcp_servers=[server])

    assert params_of(log, "session/new")["mcpServers"] == [server]


@asyncio_test
async def test_a_session_the_agent_did_not_name_is_an_error() -> None:
    async with connected("--nameless-session") as client:
        with pytest.raises(ACPSessionError, match="without naming it") as caught:
            await client.new_session()

    assert caught.value.operation == "session/new"


@asyncio_test
async def test_resuming_asks_the_agent_to_load_the_session(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    async with connected(log=log) as client:
        session = await client.resume_session("sess_fake_1")
        assert session.session_id == "sess_fake_1"

    assert params_of(log, "session/load")["sessionId"] == "sess_fake_1"


@asyncio_test
async def test_resuming_an_agent_that_cannot_fails_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    """Capability negotiation exists so this is not discovered mid-turn."""
    log = tmp_path / "sent.jsonl"
    async with connected("--no-resume", log=log) as client:
        with pytest.raises(ACPAgentCapabilityError, match="loadSession") as caught:
            await client.resume_session("sess_fake_1")

    assert caught.value.session_id == "sess_fake_1"
    assert "session/load" not in methods(log)


@asyncio_test
async def test_a_turn_streams_while_it_runs_and_ends_by_saying_so() -> None:
    async with connected() as client:
        session = await client.new_session()
        events = [event async for event in session.prompt("Review this change")]

    assert [event.type for event in events] == [
        ACPEventType.MESSAGE_DELTA,
        ACPEventType.RAW,
        ACPEventType.RAW,
        ACPEventType.PROMPT_COMPLETED,
    ]
    assert events[0].data["content"] == {"type": "text", "text": "Looking."}
    assert events[-1].data == {"stopReason": "end_turn"}


@asyncio_test
async def test_streamed_events_say_whose_they_are() -> None:
    async with connected() as client:
        session = await client.new_session()
        events = [event async for event in session.prompt("hello")]

    assert {event.agent for event in events} == {"fake"}
    assert {event.session_id for event in events} == {"sess_fake_1"}
    assert events[0].name == "acp.message.delta"


@asyncio_test
async def test_an_update_this_release_does_not_know_arrives_as_raw() -> None:
    """Forward compatibility: an ACP addition must not stop a turn."""
    async with connected() as client:
        session = await client.new_session()
        events = [event async for event in session.prompt("hello")]

    unknown, malformed = (
        event for event in events if event.type == ACPEventType.RAW
    )
    assert unknown.data["sessionUpdate"] == "a_kind_invented_after_this_release"
    # And an update that is not even shaped like one still reaches the consumer
    # rather than taking the connection down on the read loop.
    assert malformed.data["method"] == "session/update"


@asyncio_test
async def test_text_is_sent_as_a_content_block(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    async with connected(log=log) as client:
        session = await client.new_session()
        async for _ in session.prompt("Review this change"):
            pass

    assert params_of(log, "session/prompt")["prompt"] == [
        {"type": "text", "text": "Review this change"}
    ]


@asyncio_test
async def test_content_blocks_are_sent_as_assembled(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    blocks = [{"type": "text", "text": "look at"}, {"type": "resource_link", "uri": "f"}]
    async with connected(log=log) as client:
        session = await client.new_session()
        async for _ in session.prompt(blocks):
            pass

    assert params_of(log, "session/prompt")["prompt"] == blocks


@asyncio_test
async def test_a_refused_prompt_names_the_session_that_was_refused() -> None:
    async with connected("--refuse-prompt") as client:
        session = await client.new_session()

        with pytest.raises(ACPSessionError, match="over quota") as caught:
            async for _ in session.prompt("hello"):
                pass

    assert caught.value.operation == "session/prompt"
    assert caught.value.session_id == "sess_fake_1"


@asyncio_test
async def test_a_permission_request_is_streamed_and_answered() -> None:
    """Answered rather than ignored: an unanswered request hangs the turn."""
    async with connected("--permission") as client:
        session = await client.new_session()
        events = [event async for event in session.prompt("do something")]

    requested = next(
        event for event in events if event.type == ACPEventType.PERMISSION_REQUESTED
    )
    assert requested.data["toolCall"] == {"toolCallId": "call_1"}

    answered = next(
        event for event in events if event.type == ACPEventType.TOOL_UPDATED
    )
    assert answered.data["answer"] == {"outcome": {"outcome": "cancelled"}}


@asyncio_test
async def test_a_method_this_client_does_not_implement_is_refused_not_ignored() -> None:
    """An unanswered request would hang the turn, and then the graph."""
    async with connected("--read-file") as client:
        assert isinstance(client, ACPClient)
        session = await client.new_session()
        events = [event async for event in session.prompt("read something")]

    answered = next(event for event in events if event.type == ACPEventType.TOOL_UPDATED)
    refusal = answered.data["refusal"]
    assert isinstance(refusal, dict)
    assert refusal["code"] == -32601
    assert events[-1].type == ACPEventType.PROMPT_COMPLETED


@asyncio_test
async def test_one_turn_at_a_time_in_a_session() -> None:
    async with connected("--slow") as client:
        session = await client.new_session()
        async with aclosing(session.prompt("first")) as turn:
            await anext(turn)

            with pytest.raises(ACPSessionError, match="already running"):
                async for _ in session.prompt("second"):
                    pass


@asyncio_test
async def test_abandoning_a_turn_tells_the_agent_to_stop(tmp_path: Path) -> None:
    """Otherwise the agent keeps working for a consumer that stopped listening."""
    log = tmp_path / "sent.jsonl"
    async with connected("--slow", log=log) as client:
        session = await client.new_session()
        async with aclosing(session.prompt("start something long")) as turn:
            assert (await anext(turn)).type == ACPEventType.MESSAGE_DELTA

    assert params_of(log, "session/cancel") == {"sessionId": "sess_fake_1"}


@asyncio_test
async def test_cancelling_stops_the_turn_and_keeps_the_session(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    async with connected("--slow", log=log) as client:
        session = await client.new_session()
        async with aclosing(session.prompt("start something long")) as turn:
            await anext(turn)
            await session.cancel()
            remaining = [event async for event in turn]

        assert remaining[-1].data == {"stopReason": "cancelled"}
        assert session.session_id == "sess_fake_1"

    assert methods(log).count("session/cancel") == 1


@asyncio_test
async def test_a_session_can_run_a_second_turn() -> None:
    async with connected() as client:
        session = await client.new_session()

        for _ in range(2):
            events = [event async for event in session.prompt("again")]
            assert events[-1].data == {"stopReason": "end_turn"}


@asyncio_test
async def test_an_agent_that_dies_says_how_it_died() -> None:
    async with connected("--die-on-new-session") as client:
        with pytest.raises(ACPConnectionError, match="everything is on fire") as caught:
            await client.new_session()

    assert caught.value.agent == "fake"
    assert caught.value.operation == "session/new"
    assert "status 3" in str(caught.value)


@asyncio_test
async def test_a_command_that_does_not_exist_says_which_one() -> None:
    provider = StdioACPProvider(name="missing", command=["definitely-not-an-agent"])

    with pytest.raises(ACPConnectionError, match="definitely-not-an-agent") as caught:
        await provider.connect()

    assert caught.value.agent == "missing"


@asyncio_test
async def test_closing_twice_is_allowed_and_closing_once_is_enough() -> None:
    client = await fake_agent().connect()
    await client.close()
    await client.close()

    with pytest.raises(ACPConnectionError, match="is closed"):
        await client.new_session()


@asyncio_test
async def test_several_sessions_share_one_connection() -> None:
    """One agent process, several conversations -- the reason the two are split."""
    async with connected() as client:
        first = await client.new_session()
        second = await client.resume_session("sess_fake_1")

        await first.close()
        events = [event async for event in second.prompt("still here?")]

    assert events[-1].type == ACPEventType.PROMPT_COMPLETED


@asyncio_test
async def test_events_carry_no_reference_to_the_agent_s_own_containers() -> None:
    """An event is streamed to consumers that may keep or edit what they see."""
    async with connected() as client:
        session = await client.new_session()
        events: list[ACPEvent] = [event async for event in session.prompt("hello")]

    payload = events[0].to_dict()["data"]
    assert isinstance(payload, dict)
    payload["content"] = "MUTATED"
    assert events[0].data["content"] == {"type": "text", "text": "Looking."}
