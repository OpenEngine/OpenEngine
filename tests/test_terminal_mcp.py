"""Run-bound MCP terminal tools and their single-result invariants."""

import asyncio

from engine.domain import (
    AgentId,
    AgentRunId,
    RunFailed,
    RunId,
    StepCompleted,
    StepId,
    StepSpec,
)
from engine.runtime.terminal_mcp import (
    TerminalMcpBroker,
    TerminalResultRegistry,
    _mcp_response,
)


STEP = StepSpec(StepId("implementation"), AgentId("coder"), ("revision",))


def _request(
    broker: TerminalMcpBroker,
    request_id: str | int,
    name: str,
    arguments: object,
) -> dict[str, object]:
    # This helper stands in for the stdio bridge. The model never receives the
    # opaque token or any workflow identifier.
    config = broker.config
    token = config.args[config.args.index("--token") + 1]
    return {
        "token": token,
        "request_id": request_id,
        "name": name,
        "arguments": arguments,
    }


def test_completion_uses_bound_ids_and_records_the_mcp_request() -> None:
    async def scenario() -> None:
        delivered: list[StepCompleted | RunFailed] = []

        async def deliver(event: StepCompleted | RunFailed) -> None:
            delivered.append(event)

        broker = TerminalMcpBroker(
            run_id=RunId("bound-run"),
            agent_run_id=AgentRunId("bound-agent-run"),
            step=STEP,
            registry=TerminalResultRegistry(),
            deliver=deliver,
        )
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "mcp-42",
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )
            event = await broker.result()

        assert response == {"ok": True, "acknowledgement": "accepted"}
        assert delivered == [event], "delivery must finish before acknowledgement"
        assert isinstance(event, StepCompleted)
        assert event.run_id == "bound-run"
        assert event.agent_run_id == "bound-agent-run"
        assert event.step_id == STEP.step_id
        assert event.mcp_request_id == "mcp-42"

    asyncio.run(scenario())


def test_model_supplied_identifiers_are_rejected_not_trusted() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("bound-run"),
            agent_run_id=AgentRunId("bound-agent-run"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    1,
                    "complete_step",
                    {
                        "run_id": "other-run",
                        "step_id": "other-step",
                        "outcome": "success",
                        "summary": "Done.",
                    },
                )
            )
        assert response["ok"] is False
        assert "exactly outcome" in str(response["error"])

    asyncio.run(scenario())


def test_duplicate_and_conflicting_terminal_calls_are_rejected() -> None:
    async def scenario() -> None:
        registry = TerminalResultRegistry()
        first = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=registry,
        )
        async with first:
            accepted = await first._submit(
                _request(
                    first,
                    1,
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )
            duplicate = await first._submit(
                _request(first, 1, "fail_step", {"summary": "Changed my mind."})
            )

        other_run = TerminalMcpBroker(
            run_id=RunId("run-2"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=registry,
        )
        async with other_run:
            cross_run = await other_run._submit(
                _request(other_run, 2, "fail_step", {"summary": "No."})
            )

        assert accepted["ok"] is True
        assert duplicate["ok"] is False
        assert cross_run["ok"] is False
        assert "already accepted" in str(cross_run["error"])

    asyncio.run(scenario())


def test_fail_step_is_bound_and_auditable() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            response = await broker._submit(
                _request(broker, 99, "fail_step", {"summary": "Tests cannot pass."})
            )
            event = await broker.result()

        assert response["ok"] is True
        assert event == RunFailed(
            run_id=RunId("run-1"),
            reason="Tests cannot pass.",
            agent_run_id=AgentRunId("agent-run-1"),
            mcp_request_id=99,
        )

    asyncio.run(scenario())


def test_stdio_mcp_surface_lists_only_terminal_tools() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
        )
    )

    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["complete_step", "fail_step"]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_stdio_bridge_returns_a_small_acknowledgement() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            config = broker.config
            host = config.args[config.args.index("--host") + 1]
            port = int(config.args[config.args.index("--port") + 1])
            token = config.args[config.args.index("--token") + 1]
            response = await _mcp_response(
                host,
                port,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {
                        "name": "complete_step",
                        "arguments": {
                            "outcome": "success",
                            "summary": "Done.",
                            "outputs": {"revision": "abc123"},
                        },
                    },
                },
            )

        assert response == {
            "jsonrpc": "2.0",
            "id": "call-1",
            "result": {
                "content": [{"type": "text", "text": "accepted"}],
                "structuredContent": {"accepted": True},
            },
        }

    asyncio.run(scenario())
