"""Run-bound MCP terminal tools and their single-result invariants."""

import asyncio

from engine.domain import (
    AgentId,
    AgentRunId,
    ApprovalDecision,
    ApprovalKind,
    RunFailed,
    RunId,
    StepCompleted,
    StepId,
    StepSpec,
    WorkspaceId,
)
from engine.ports import ApprovalRequest, GitResult
from engine.runtime.terminal_mcp import (
    DEFAULT_BASE_REF,
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


def _direct_request(
    broker: TerminalMcpBroker,
    request_id: str | int,
    name: str,
    arguments: object,
) -> dict[str, object]:
    """Call the in-process broker without starting its loopback bridge."""

    return {
        "token": broker._token,
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


def test_stdio_mcp_surface_includes_non_terminal_clarify_tool() -> None:
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
    assert [tool["name"] for tool in tools] == [
        "complete_step",
        "fail_step",
        "clarify",
    ]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_clarify_acknowledges_without_submitting_a_terminal_result() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            clarified = await broker._submit(
                _request(broker, "clarify-1", "clarify", {})
            )
            assert broker._result is not None
            assert not broker._result.done()
            completed = await broker._submit(
                _request(
                    broker,
                    "complete-1",
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done after clarifying.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )

        assert clarified == {"ok": True, "acknowledgement": "clarified"}
        assert completed == {"ok": True, "acknowledgement": "accepted"}

    asyncio.run(scenario())


def test_reviewer_mcp_surface_includes_repo_comment_tool() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            repository_tools=("add_comment",),
        )
    )

    assert response is not None
    tools = response["result"]["tools"]
    add_comment = next(tool for tool in tools if tool["name"] == "add_comment")
    assert add_comment["inputSchema"]["required"] == ["pr_url", "comment"]
    assert add_comment["inputSchema"]["dependentRequired"] == {
        "file": ["line"],
        "line": ["file"],
    }


def test_repo_comment_is_forwarded_before_review_can_complete() -> None:
    class RecordingSourceControl:
        def __init__(self) -> None:
            self.comments: list[tuple[object, ...]] = []

        async def add_comment(self, *arguments: object) -> None:
            self.comments.append(arguments)

    async def scenario() -> None:
        source_control = RecordingSourceControl()
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(source_control, ("add_comment",))  # type: ignore[arg-type]
        broker._result = asyncio.get_running_loop().create_future()
        request = {
            "token": broker._token,
            "request_id": "comment-1",
            "name": "complete_step",
            "arguments": {
                "outcome": "success",
                "summary": "Done.",
                "outputs": {"revision": "abc123"},
            },
        }

        refused = await broker._submit(request)
        request["name"] = "add_comment"
        request["arguments"] = {
            "pr_url": "https://github.com/acme/api/pull/42",
            "comment": "This can race.",
            "file": "src/worker.py",
            "line": 17,
        }
        accepted = await broker._submit(request)

        assert refused["ok"] is False
        assert accepted == {"ok": True, "acknowledgement": "comment added"}
        assert source_control.comments == [
            (
                "https://github.com/acme/api/pull/42",
                "This can race.",
                "src/worker.py",
                17,
            )
        ]

    asyncio.run(scenario())


class RecordingRepository:
    """A source control that answers instead of touching a repository."""

    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.git_calls: list[tuple[object, tuple[str, ...]]] = []
        self.reviews: list[tuple[object, ...]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    async def run_git(self, workspace_id, arguments) -> GitResult:
        self.git_calls.append((workspace_id, tuple(arguments)))
        return GitResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr="" if not self._exit_code else "fatal: no",
        )

    async def request_review(self, workspace_id, branch, base_ref, title, body) -> str:
        self.reviews.append((workspace_id, branch, base_ref, title, body))
        return "https://github.com/acme/api/pull/7"


async def _approve_git(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision.ACCEPT


def _repository_broker(
    source_control: object,
    *names: str,
    git_approval=_approve_git,
    tool_call_ids=None,
) -> TerminalMcpBroker:
    broker = TerminalMcpBroker(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("agent-run-1"),
        step=STEP,
        registry=TerminalResultRegistry(),
    )
    broker.enable_repository_tools(
        source_control,  # type: ignore[arg-type]
        names,
        WorkspaceId("ws-under-test"),
        git_approval,
        tool_call_ids,
    )
    return broker


def test_repository_tools_are_listed_only_when_the_step_holds_them() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            repository_tools=("git_subcommand", "open_pull_request"),
        )
    )

    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert "add_comment" not in tools
    git = tools["git_subcommand"]
    assert git["inputSchema"]["properties"]["arguments"] == {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
    }
    assert tools["open_pull_request"]["inputSchema"]["required"] == [
        "branch",
        "title",
        "body",
    ]


def test_git_runs_against_the_step_workspace_and_returns_what_it_printed() -> None:
    """The model names the command; the broker names the workspace.

    A model that could pass a workspace id could name somebody else's, so the
    one the step is running in is bound when the tool is enabled and never
    appears in the tool's schema.
    """

    async def scenario() -> None:
        source_control = RecordingRepository(stdout="On branch agent/greeting")
        broker = _repository_broker(source_control, "git_subcommand")
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "git-1",
                    "git_subcommand",
                    {"arguments": ["status", "--short", "--branch"]},
                )
            )

        assert response == {
            "ok": True,
            "acknowledgement": "git ran",
            "output": "On branch agent/greeting",
        }
        assert source_control.git_calls == [
            (WorkspaceId("ws-under-test"), ("status", "--short", "--branch"))
        ]

    asyncio.run(scenario())


def test_git_is_approved_by_engine_even_when_the_mcp_tool_was_preapproved() -> None:
    """Provider permission only admits the call to this server.

    Git may launch helpers, hooks and aliases outside the provider sandbox, so
    the server independently submits the exact argument vector to Engine's
    approval broker before invoking source control.
    """

    async def scenario() -> None:
        requests: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            requests.append(request)
            return ApprovalDecision.ACCEPT

        source_control = RecordingRepository(stdout="clean")
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=approve
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-sensitive",
                "git_subcommand",
                {"arguments": ["-c", "alias.x=!sh", "x"]},
            )
        )

        assert response["ok"] is True
        assert len(requests) == 1
        request = requests[0]
        assert request.kind is ApprovalKind.TOOL_USE
        assert request.tool_name == "mcp__workflow__git_subcommand"
        # Nothing told this broker what the provider called the call, and the
        # request id is this server's own numbering -- so the request names no
        # call rather than one nothing in the transcript answers to.
        assert request.tool_call_id is None
        assert request.command == "git -c 'alias.x=!sh' x"
        assert request.arguments == '{"arguments": ["-c", "alias.x=!sh", "x"]}'
        assert request.allowed_decisions == (
            ApprovalDecision.ACCEPT,
            ApprovalDecision.CANCEL,
        )
        assert source_control.git_calls == [
            (WorkspaceId("ws-under-test"), ("-c", "alias.x=!sh", "x"))
        ]

    asyncio.run(scenario())


def test_git_approval_names_the_call_the_provider_reported() -> None:
    """The pause has to be readable beside the call it interrupted.

    A client pairs the two by the provider's id for the call, and this server
    is reached over a transport of its own -- so the id is looked up from what
    the provider streamed rather than taken from the MCP request carrying it.
    """

    async def scenario() -> None:
        requests: list[ApprovalRequest] = []
        asked: list[tuple[str, str]] = []

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            requests.append(request)
            return ApprovalDecision.ACCEPT

        def tool_call_ids(name: str, arguments: str) -> str | None:
            asked.append((name, arguments))
            return "thread-1:item-7"

        source_control = RecordingRepository(stdout="clean")
        broker = _repository_broker(
            source_control,
            "git_subcommand",
            git_approval=approve,
            tool_call_ids=tool_call_ids,
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-9",
                "git_subcommand",
                {"arguments": ["status", "--short"]},
            )
        )

        assert response["ok"] is True
        assert requests[0].tool_call_id == "thread-1:item-7"
        # Asked by what the call is, so the answer can be found among the calls
        # the provider has reported rather than among this server's requests.
        assert asked == [
            (
                "mcp__workflow__git_subcommand",
                '{"arguments": ["status", "--short"]}',
            )
        ]

    asyncio.run(scenario())


def test_git_fails_closed_when_the_step_has_no_approval_handler() -> None:
    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=None
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-1",
                "git_subcommand",
                {"arguments": ["status"]},
            )
        )

        assert response["ok"] is False
        assert "requires approval handling" in str(response["error"])
        assert source_control.git_calls == []

    asyncio.run(scenario())


def test_git_does_not_run_when_approval_is_cancelled() -> None:
    async def cancel(_request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.CANCEL

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=cancel
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-1",
                "git_subcommand",
                {"arguments": ["push", "origin", "agent/topic"]},
            )
        )

        assert response == {"ok": False, "error": "git_subcommand was not approved"}
        assert source_control.git_calls == []

    asyncio.run(scenario())


def test_a_failing_git_command_is_reported_with_what_git_said() -> None:
    async def scenario() -> None:
        broker = _repository_broker(
            RecordingRepository(exit_code=128), "git_subcommand"
        )
        async with broker:
            response = await broker._submit(
                _request(broker, "git-1", "git_subcommand", {"arguments": ["push"]})
            )

        assert response["ok"] is False
        assert "git exited 128" in str(response["error"])
        assert "fatal: no" in str(response["error"])

    asyncio.run(scenario())


def test_a_repository_tool_the_step_was_not_granted_is_refused() -> None:
    """Serving is per step, so holding one tool is not holding the rest."""

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(source_control, "git_subcommand")
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "pr-1",
                    "open_pull_request",
                    {
                        "branch": "agent/greeting",
                        "title": "feat: greet",
                        "body": "Body.",
                    },
                )
            )

        assert response["ok"] is False
        assert "not enabled" in str(response["error"])
        assert source_control.reviews == []

    asyncio.run(scenario())


def test_a_pull_request_url_reaches_the_model_through_the_bridge() -> None:
    """A bare "accepted" would leave the step guessing at its own output."""

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(source_control, "open_pull_request")
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
                        "name": "open_pull_request",
                        "arguments": {
                            "branch": "agent/greeting",
                            "title": "feat: greet",
                            "body": "Body.",
                        },
                    },
                },
                repository_tools=("open_pull_request",),
            )

        assert response is not None
        url = "https://github.com/acme/api/pull/7"
        assert response["result"]["content"] == [{"type": "text", "text": url}]
        assert response["result"]["structuredContent"] == {
            "accepted": True,
            "output": url,
        }
        # The base nobody named, rather than nothing at all.
        assert source_control.reviews == [
            (
                WorkspaceId("ws-under-test"),
                "agent/greeting",
                DEFAULT_BASE_REF,
                "feat: greet",
                "Body.",
            )
        ]
        assert config.args.count("--repository-tool") == 1

    asyncio.run(scenario())


def test_the_bridge_credential_can_never_read_as_a_command_line_flag() -> None:
    """The token is an argv element, so its alphabet is a correctness property.

    `token_urlsafe` draws from an alphabet that includes `-`, and a token that
    began with one was read by the server's own parser as an option: it exited
    on `--token: expected one argument` before answering `initialize`. About
    one agent run in sixty-four, and nothing in the aftermath named the cause
    -- the step's agent simply had no tools, and the run failed two
    corrections later.

    Sampled rather than asserted once, because a one-in-sixty-four fault is
    not something a single draw catches.
    """

    async def scenario() -> list[str]:
        tokens: list[str] = []
        for index in range(200):
            broker = TerminalMcpBroker(
                run_id=RunId(f"run-{index}"),
                agent_run_id=AgentRunId(f"agent-run-{index}"),
                step=STEP,
                registry=TerminalResultRegistry(),
            )
            async with broker:
                config = broker.config
                tokens.append(config.args[config.args.index("--token") + 1])
        return tokens

    tokens = asyncio.run(scenario())

    assert all(token.isalnum() for token in tokens)
    # And it is a credential, so no two sessions share one.
    assert len(set(tokens)) == len(tokens)


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
