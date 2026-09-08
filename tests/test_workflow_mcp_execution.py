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
    ApprovalDecision,
    Message,
    RunId,
    StartAgentRun,
    StepCompleted,
    StepId,
    StepSpec,
    ToolCall,
    ToolSpec,
    WorkspaceId,
)
from engine.ports import ApprovalRequest, AgentTurn, GitResult, McpServerConfig
from engine.runtime import (
    Capabilities,
    Dispatcher,
    GRANTED_TOOLS_NOTE,
    INVALID_COMPLETION_ERROR,
    terminal_tool_names,
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


def _capabilities(runner: object, source_control: object | None = None) -> Capabilities:
    missing = object()
    return Capabilities(
        workflow_runtime=missing,
        source_control=source_control if source_control is not None else missing,
        agent_runner=runner,
        communications=missing,
        workspace_provider=missing,
        state_store=InMemoryStateStore(),
    )


class CommentingSourceControl:
    """Enough of the port for the broker to serve `add_comment`."""

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:  # pragma: no cover - presence is what the broker checks
        pass


async def _call_tool(
    mcp_server: McpServerConfig,
    name: str,
    arguments: dict[str, object],
    request_id: str = "tool-call-7",
) -> dict[str, object]:
    """Call one tool on the run-bound server the way a real CLI would."""
    host = mcp_server.args[mcp_server.args.index("--host") + 1]
    port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
    token = mcp_server.args[mcp_server.args.index("--token") + 1]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


async def _report_completion(mcp_server: McpServerConfig) -> None:
    """Call `complete_step`, having commented first if the step is a review.

    The broker refuses to complete a commenting step that left no comment, so
    a runner that skips it is not modelling what a reviewer does.
    """
    if "add_comment" in mcp_server.args:
        commented = await _call_tool(
            mcp_server,
            "add_comment",
            {"pr_url": "https://example.invalid/pr/1", "comment": "Looks right."},
            request_id="tool-call-6",
        )
        assert commented["acknowledgement"] == "comment added"
    acknowledgement = await _call_tool(
        mcp_server,
        "complete_step",
        {"outcome": "success", "summary": "Done.", "outputs": {}},
    )
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


class ProfileCapturingRunner(CallingMcpRunner):
    """Keeps the profile each turn was actually run with."""

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


REVIEWER = AgentProfile(
    AgentId("reviewer"), "Review the change.", capabilities=("add_comment",)
)
REVIEW_COMMAND = replace(
    COMMAND, profile=REVIEWER, step=StepSpec(StepId("review"), REVIEWER.agent_id)
)


def test_a_workflow_step_is_told_every_tool_its_server_serves() -> None:
    """Everything the broker lists, not everything the profile declares.

    A reviewer granted `add_comment` and never told it holds one reports the
    comment it would have left, which is the same failure as a planner that
    describes a milestone instead of recording it. The terminal tools are on
    the same footing: the broker serves them, so a note introduced as an
    enumeration has to name them too or it is one the model can read as
    complete and be wrong.
    """

    async def scenario() -> None:
        runner = ProfileCapturingRunner()
        capabilities = _capabilities(runner, CommentingSourceControl())

        await Dispatcher(capabilities).run_workflow_agent(
            REVIEW_COMMAND, runner=runner
        )

        instructions = runner.profiles[0].instructions
        assert instructions.startswith("Review the change.")
        assert GRANTED_TOOLS_NOTE in instructions
        listed = {line[2:] for line in instructions.splitlines() if line.startswith("- ")}
        assert listed == set(terminal_tool_names(("add_comment",)))
        assert "add_comment" in listed

    asyncio.run(scenario())


def test_a_grant_the_broker_cannot_serve_is_not_announced() -> None:
    """`add_comment` is served only when source control can honour it.

    Granting it against a source control that cannot comment leaves the tool
    off the MCP listing, and a system prompt naming it anyway would send the
    reviewer after a tool that is not there -- ending turns without a terminal
    result until the correction budget runs out.
    """

    async def scenario() -> None:
        runner = ProfileCapturingRunner()

        # `_capabilities` defaults source control to a bare `object()`, which
        # is exactly the case: granted, and unservable.
        await Dispatcher(_capabilities(runner)).run_workflow_agent(
            REVIEW_COMMAND, runner=runner
        )

        instructions = runner.profiles[0].instructions
        assert "add_comment" not in instructions
        listed = {line[2:] for line in instructions.splitlines() if line.startswith("- ")}
        assert listed == set(terminal_tool_names())

    asyncio.run(scenario())


class GitSourceControl:
    """Enough of the port for the broker to serve `git_subcommand`."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[str, ...]]] = []

    async def run_git(self, workspace_id, arguments):
        self.calls.append((workspace_id, tuple(arguments)))
        return GitResult(exit_code=0, stdout="nothing to commit", stderr="")


IMPLEMENTER = AgentProfile(
    AgentId("coder"),
    "Implement the change.",
    capabilities=("git_subcommand",),
)
IMPLEMENTATION_COMMAND = replace(
    COMMAND, profile=IMPLEMENTER, workspace_id=WorkspaceId("ws-1")
)


class GitCallingMcpRunner(CallingMcpRunner):
    """Runs one git command before reporting the step complete."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[dict[str, object]] = []

    async def run_turn_with_mcp(
        self, agent_run_id, profile, messages, mcp_server, workspace_id=None
    ):
        self.answers.append(
            await _call_tool(
                mcp_server,
                "git_subcommand",
                {"arguments": ["status", "--porcelain"]},
                request_id="tool-call-5",
            )
        )
        return await super().run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )

    async def run_turn_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ):
        raise AssertionError("workflow execution should use MCP")

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_approval,
        on_message=None,
        workspace_id=None,
    ):
        return await self.run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )


def test_git_reaches_the_step_own_workspace_without_the_model_naming_one() -> None:
    """The step's workspace is bound by the dispatcher, not passed by the model.

    It is the only reason the tool can be handed a whole `git` and still be
    bounded: a model that could name a workspace could name another step's.
    """

    async def scenario() -> None:
        runner = GitCallingMcpRunner()
        source_control = GitSourceControl()
        approvals: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            approvals.append(request)
            return ApprovalDecision.ACCEPT

        await Dispatcher(_capabilities(runner, source_control)).run_workflow_agent(
            IMPLEMENTATION_COMMAND, runner=runner, on_approval=approve
        )

        assert source_control.calls == [
            (WorkspaceId("ws-1"), ("status", "--porcelain"))
        ]
        assert runner.answers[0]["output"] == "nothing to commit"
        assert [request.tool_name for request in approvals] == [
            "mcp__workflow__git_subcommand"
        ]

    asyncio.run(scenario())


#: How both adapters spell one call to this server. The dispatcher pairs an
#: approval to a call by exact equality on these two strings, so the spelling
#: is a contract between the adapters and the pairing rather than a detail --
#: which is what `test_both_adapters_spell_a_git_call_this_way` is for.
GIT_TOOL = "mcp__workflow__git_subcommand"
STATUS = ("status", "--porcelain")
STATUS_ARGUMENTS = json.dumps({"arguments": list(STATUS)}, sort_keys=True)


def _reported_git_call(call_id: str, arguments: str = STATUS_ARGUMENTS) -> Message:
    """The assistant message an adapter writes when the provider makes a call."""
    return Message.assistant(tool_calls=(ToolCall(call_id, GIT_TOOL, arguments),))


class ScriptedGitRunner(CallingMcpRunner):
    """Streams what the provider reported, then calls what it reported calling.

    The order is the fixture's whole point: a CLI writes the call onto its own
    stream before invoking the tool over MCP, and the id an approval is paired
    by exists only in the first of those. Each entry is what to stream and the
    git arguments to then call with, so a test can vary or drop the streamed
    half and get the cases a browser cannot produce on purpose.
    """

    def __init__(
        self, script: Sequence[tuple[Sequence[Message], Sequence[str]]]
    ) -> None:
        super().__init__()
        self._script = script
        self.answers: list[dict[str, object]] = []

    async def run_turn_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ):
        raise AssertionError("workflow execution should use MCP")

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        on_approval,
        on_message=None,
        workspace_id=None,
    ):
        assert on_message is not None
        for index, (reported, arguments) in enumerate(self._script):
            for message in reported:
                on_message(message)
            self.answers.append(
                await _call_tool(
                    mcp_server,
                    "git_subcommand",
                    {"arguments": list(arguments)},
                    request_id=f"git-{index}",
                )
            )
        return await super().run_turn_with_mcp(
            agent_run_id, profile, messages, mcp_server, workspace_id
        )


async def _paired_call_ids(
    script: Sequence[tuple[Sequence[Message], Sequence[str]]],
) -> list[str | None]:
    """Run the step and report which call each git approval was paired with."""
    runner = ScriptedGitRunner(script)
    approvals: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        approvals.append(request)
        return ApprovalDecision.ACCEPT

    await Dispatcher(_capabilities(runner, GitSourceControl())).run_workflow_agent(
        IMPLEMENTATION_COMMAND, runner=runner, on_approval=approve
    )
    return [request.tool_call_id for request in approvals]


def test_a_git_approval_is_paired_with_the_call_the_provider_reported() -> None:
    """The id comes off the provider's stream, not off the MCP request.

    This server is reached over a transport of its own, and the number it
    numbers a request with is not anything the conversation contains -- so a
    request that named it paired with nothing and was shown at the end of the
    turn instead of beside the command it was asking about.
    """

    async def scenario() -> None:
        assert await _paired_call_ids(
            [((Message.assistant("Checking."), _reported_git_call("call-7")), STATUS)]
        ) == ["call-7"]

    asyncio.run(scenario())


def test_a_git_approval_names_no_call_when_nothing_the_provider_said_matches() -> None:
    """Unmatched is `None` rather than the nearest thing.

    A request rendered beside a call it was not about tells the reader the
    agent asked to run something it never asked to run, which is worse than
    the end-of-turn slot `None` falls back to. So a different command, and a
    call the provider has not reported at all, both decline to pair.
    """

    async def scenario() -> None:
        other = json.dumps({"arguments": ["log", "--oneline"]}, sort_keys=True)
        assert await _paired_call_ids(
            [
                ((_reported_git_call("call-1", other),), STATUS),
                ((), STATUS),
            ]
        ) == [None, None]

    asyncio.run(scenario())


def test_a_repeated_git_call_is_paired_with_the_one_just_reported() -> None:
    """Two identical calls are told apart by which was reported most recently.

    The case the browser test deliberately avoids: it drives two *different*
    commands, because an id that anchors nothing and an id that anchors the
    wrong call both read as inline when there is one call on screen to be
    beside. Identical calls are the only input where the newest-first scan can
    pair with the wrong one, so it is pinned here instead.
    """

    async def scenario() -> None:
        assert await _paired_call_ids(
            [
                ((_reported_git_call("call-1"),), STATUS),
                ((_reported_git_call("call-2"),), STATUS),
            ]
        ) == ["call-1", "call-2"]

    asyncio.run(scenario())


def test_both_adapters_spell_a_git_call_this_way() -> None:
    """The pairing is exact string equality, so the spelling is a contract.

    The dispatcher matches on the tool name and argument text the adapter
    wrote. Both write `json.dumps(..., sort_keys=True)`; compact separators or
    `ensure_ascii=False` on either side would pair nothing and send every
    repository approval quietly back to the end of the turn. Nothing else
    checks the two spellings against each other, so this asserts both against
    the same strings the tests above stream.
    """

    # Imported here rather than at the top: this module is about runtime
    # delivery, and the adapters appear only as the other half of this one
    # contract.
    from engine.adapters.agent_runner.claude_code import (
        messages_from_event as claude_messages_from_event,
    )
    from engine.adapters.agent_runner.codex import (
        messages_from_event as codex_messages_from_event,
    )

    claude = claude_messages_from_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": GIT_TOOL,
                        "input": {"arguments": list(STATUS)},
                    }
                ]
            },
        }
    )
    codex = codex_messages_from_event(
        {
            "type": "item.started",
            "item": {
                "id": "item-1",
                "type": "mcp_tool_call",
                "server": "workflow",
                "tool": "git_subcommand",
                "arguments": json.dumps({"arguments": list(STATUS)}),
            },
        },
        thread_id="thread-1",
    )

    for call in (claude[0].tool_calls[0], codex[0].tool_calls[0]):
        assert call.name == GIT_TOOL
        assert call.arguments == STATUS_ARGUMENTS


def test_a_non_mcp_step_announces_nothing_because_it_serves_nothing() -> None:
    """The plain `run_turn` branch passes no `tools=` and has no broker."""

    class PlainRunner:
        permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

        def __init__(self) -> None:
            self.profiles: list[AgentProfile] = []

        async def run_turn(
            self,
            agent_run_id: AgentRunId,
            profile: AgentProfile,
            messages: Sequence[Message],
            tools: Sequence[ToolSpec] = (),
            workspace_id: str | None = None,
        ) -> AgentTurn:
            self.profiles.append(profile)
            return AgentTurn(Message.assistant("Reviewed."))

    async def scenario() -> None:
        runner = PlainRunner()

        await Dispatcher(_capabilities(runner, CommentingSourceControl())).run_workflow_agent(
            REVIEW_COMMAND, runner=runner
        )

        assert runner.profiles[0].instructions == "Review the change."

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
