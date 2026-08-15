"""Runtime delivery and process-lifecycle behavior for terminal MCP calls."""

import asyncio
import json
from collections.abc import Sequence

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    Message,
    RunId,
    StartAgentRun,
    StepCompleted,
    StepId,
    StepSpec,
    ToolSpec,
)
from engine.ports import AgentTurn, McpServerConfig
from engine.runtime import Capabilities, Dispatcher


PROFILE = AgentProfile(AgentId("coder"), "Implement the requested change.")
COMMAND = StartAgentRun(
    run_id=RunId("run-1"),
    agent_run_id=AgentRunId("agent-run-1"),
    instance_id=AgentInstanceId("instance-1"),
    profile=PROFILE,
    prompt="Go.",
    step=StepSpec(StepId("implementation"), PROFILE.agent_id),
)


def _capabilities(runner: object) -> Capabilities:
    missing = object()
    return Capabilities(
        workflow_runtime=missing,
        source_control=missing,
        agent_runner=runner,
        communications=missing,
        workspace_provider=missing,
        state_store=InMemoryStateStore(),
    )


class CallingMcpRunner:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: str | None = None,
    ) -> AgentTurn:
        raise AssertionError("workflow execution should use MCP")

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: str | None = None,
    ) -> AgentTurn:
        host = mcp_server.args[mcp_server.args.index("--host") + 1]
        port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
        token = mcp_server.args[mcp_server.args.index("--token") + 1]
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            json.dumps(
                {
                    "token": token,
                    "request_id": "tool-call-7",
                    "name": "complete_step",
                    "arguments": {
                        "outcome": "success",
                        "summary": "Done.",
                        "outputs": {},
                    },
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        acknowledgement = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert acknowledgement["acknowledgement"] == "accepted"
        await self.cancelled.wait()
        return AgentTurn(Message.assistant("The tool call was accepted."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        assert agent_run_id == COMMAND.agent_run_id
        self.cancelled.set()


def test_accepted_result_is_delivered_then_the_cli_is_cancelled() -> None:
    async def scenario() -> None:
        runner = CallingMcpRunner()
        dispatcher = Dispatcher(_capabilities(runner))
        delivered: list[StepCompleted] = []

        async def deliver(event: StepCompleted) -> None:
            delivered.append(event)

        result = await dispatcher.run_workflow_agent(
            COMMAND, runner=runner, on_terminal_result=deliver
        )

        assert result == delivered[0]
        assert result.mcp_request_id == "tool-call-7"
        assert runner.cancelled.is_set()

    asyncio.run(scenario())


class TranscriptOnlyMcpRunner(CallingMcpRunner):
    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: str | None = None,
    ) -> AgentTurn:
        return AgentTurn(
            Message.assistant("I called complete_step."),
            steps=(Message.assistant("tool complete_step reported success"),),
        )


def test_a_transcript_claim_does_not_become_a_terminal_event() -> None:
    async def scenario() -> None:
        runner = TranscriptOnlyMcpRunner()
        result = await Dispatcher(_capabilities(runner)).run_workflow_agent(
            COMMAND, runner=runner
        )

        assert isinstance(result, AgentTurn)
        assert result.message.content == "I called complete_step."
        assert not runner.cancelled.is_set()

    asyncio.run(scenario())
