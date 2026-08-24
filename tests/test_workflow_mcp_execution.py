"""Runtime delivery and process-lifecycle behavior for terminal MCP calls."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace

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
    ToolCall,
    ToolSpec,
)
from engine.ports import AgentTurn, McpServerConfig
from engine.runtime import (
    Capabilities,
    Dispatcher,
    GRANTED_TOOLS_NOTE,
    INVALID_COMPLETION_ERROR,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


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


async def _report_completion(mcp_server: McpServerConfig) -> None:
    """Call `complete_step` on the run-bound server the way a real CLI would."""
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


class CallingMcpRunner:
    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

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
        await _report_completion(mcp_server)
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


def test_a_workflow_step_is_told_which_tools_its_profile_grants() -> None:
    """The step instructions name the terminal tools; nothing named the rest.

    A reviewer granted `add_comment` and never told it holds one reports the
    comment it would have left, which is the same failure as a planner that
    describes a milestone instead of recording it.
    """

    class ProfileCapturingRunner(CallingMcpRunner):
        def __init__(self) -> None:
            super().__init__()
            self.profiles: list[AgentProfile] = []

        async def run_turn_with_mcp(
            self, agent_run_id, profile, messages, mcp_server, workspace_id=None
        ):
            self.profiles.append(profile)
            return await super().run_turn_with_mcp(
                agent_run_id, profile, messages, mcp_server, workspace_id
            )

    async def scenario() -> None:
        runner = ProfileCapturingRunner()
        reviewer = AgentProfile(
            AgentId("reviewer"), "Review the change.", capabilities=("add_comment",)
        )
        command = replace(
            COMMAND,
            profile=reviewer,
            step=StepSpec(StepId("review"), reviewer.agent_id),
        )

        await Dispatcher(_capabilities(runner)).run_workflow_agent(
            command, runner=runner
        )

        instructions = runner.profiles[0].instructions
        assert instructions.startswith("Review the change.")
        assert GRANTED_TOOLS_NOTE in instructions
        assert "- add_comment" in instructions

    asyncio.run(scenario())


#: A step that does its work and then calls its terminal tool -- exactly the
#: shape the step instructions ask for, and the one whose last item is a call.
NARRATION = Message.assistant("Writing the greeting.")
BASH = Message.assistant(
    tool_calls=(ToolCall("bash-1", "Bash", '{"command": "echo hi > greeting.txt"}'),)
)
BASH_RESULT = Message.tool_result("bash-1", "wrote greeting.txt")
COMPLETE = Message.assistant(tool_calls=(ToolCall("tool-2", "complete_step", "{}"),))
COMPLETE_RESULT = Message.tool_result("tool-2", "accepted")


class EndsOnItsTerminalCallRunner(CallingMcpRunner):
    """Streams a turn ending in `complete_step`, then returns it reassembled.

    Both real adapters treat the last spoken text as the turn's answer and drop
    it from the steps, so when the final item is a tool call the narration that
    streamed *first* comes back last. This returns promptly rather than waiting
    to be cancelled, which is the side of the race the runtime loses when a
    provider CLI finishes before cancellation reaches it.
    """

    async def run_turn_with_mcp_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_message: object,
        workspace_id: str | None = None,
    ) -> AgentTurn:
        for message in (NARRATION, BASH, BASH_RESULT):
            on_message(message)
        await _report_completion(mcp_server)
        for message in (COMPLETE, COMPLETE_RESULT):
            on_message(message)
        return AgentTurn(
            NARRATION,
            steps=(BASH, BASH_RESULT, COMPLETE, COMPLETE_RESULT),
        )


def _stored(messages: Sequence[Message]) -> list[str]:
    """What was written, named by the tool called or the words said."""
    return [
        message.tool_calls[0].name if message.tool_calls else message.content
        for message in messages
    ]


def test_a_turn_ending_in_its_terminal_call_is_kept_in_streamed_order() -> None:
    """Reassembly moves the answer to the end; the stored conversation must not
    follow it. Comparing by position instead read that as a divergence and
    failed a step whose result had already been accepted."""

    async def scenario() -> None:
        runner = EndsOnItsTerminalCallRunner()
        capabilities = _capabilities(runner)

        result = await Dispatcher(capabilities).run_workflow_agent(
            COMMAND, runner=runner
        )

        assert isinstance(result, StepCompleted)
        conversation = await capabilities.state_store.load_conversation(
            COMMAND.instance_id
        )
        assert conversation is not None
        assert _stored(conversation.messages[1:]) == [
            "Writing the greeting.",
            "Bash",
            "wrote greeting.txt",
            "complete_step",
            "accepted",
        ]

    asyncio.run(scenario())


class RetryingMcpRunner(CallingMcpRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Message, ...]] = []

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: str | None = None,
    ) -> AgentTurn:
        self.calls.append(tuple(messages))
        if len(self.calls) == 1:
            return AgentTurn(
                Message.assistant("I called complete_step."),
                steps=(Message.assistant("tool complete_step reported success"),),
            )
        return await super().run_turn_with_mcp(
            agent_run_id,
            profile,
            messages,
            mcp_server,
            workspace_id,
        )


def test_an_invalid_exit_is_retried_with_the_completion_error() -> None:
    async def scenario() -> None:
        runner = RetryingMcpRunner()
        result = await Dispatcher(_capabilities(runner)).run_workflow_agent(
            COMMAND, runner=runner
        )

        assert isinstance(result, StepCompleted)
        assert len(runner.calls) == 2
        assert runner.calls[1][-1] == Message.user(INVALID_COMPLETION_ERROR)
        assert any(
            message.content == "I called complete_step."
            for message in runner.calls[1]
        )
        assert runner.cancelled.is_set()

    asyncio.run(scenario())


class ClarificationMcpRunner(CallingMcpRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: str | None = None,
    ) -> AgentTurn:
        self.calls += 1
        clarification = ToolCall(
            "question-1",
            "request_user_input",
            json.dumps({"question": "Which API should I preserve?"}),
        )
        return AgentTurn(
            Message.assistant("Waiting for clarification."),
            steps=(Message.assistant(tool_calls=(clarification,)),),
        )


def test_a_clarification_call_pauses_without_retrying() -> None:
    async def scenario() -> None:
        runner = ClarificationMcpRunner()
        result = await Dispatcher(_capabilities(runner)).run_workflow_agent(
            COMMAND, runner=runner
        )

        assert isinstance(result, AgentTurn)
        assert runner.calls == 1
        assert not runner.cancelled.is_set()

    asyncio.run(scenario())
