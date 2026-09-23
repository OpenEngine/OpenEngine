"""The ACP runner: the approval contract, and what each agent is configured with.

The scenarios are `test_cli_compatibility`'s own -- approve, cancel, and allow
for the session across a turn boundary -- run through the web app over
`ACPAgentRunner` against `langgraph-acp`'s fake agent. The fake really runs the
command it is allowed to, so they assert on the filesystem, and they need no
model, network or npm, so they block every pull request.

Both agents' runners are driven, with only their command pointed at the fake:
what differs between them is the configuration they send, and that is pinned
separately below from what reaches the agent.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from engine.adapters.agent_runner.acp import (
    READ_ONLY_TOOLS,
    ACPAgentRunner,
    ACPToolsUnsupportedError,
    claude_acp_runner,
    codex_acp_runner,
)
from engine.domain import (
    AgentId,
    AgentProfile,
    AgentRunId,
    ApprovalDecision,
    ApprovalKind,
    Message,
    Role,
)
from engine.domain.tools import ToolSpec
from engine.ports import (
    ApprovalRequest,
    InteractiveMcpAgentRunner,
    McpServerConfig,
    ResponseStyle,
    StreamingAgentRunner,
)
from engine.ports.permissions import ApprovalCapability
from provider_fakes import DIRECTIVE
from test_cli_compatibility import (
    FAKE_PAUSE_TIMEOUT,
    FAKE_TURN_TIMEOUT,
    SCENARIOS,
    Transcript,
    open_chat,
)

#: langgraph-acp's fake agent, in the mode that runs the prompt's `run:` command.
FAKE_AGENT = Path(__file__).resolve().parents[1] / "langgraph-acp" / "tests" / "fake_agent.py"
FAKE_COMMAND = (sys.executable, str(FAKE_AGENT), "--run-directive")

RUNNERS = {
    "codex": lambda workspace: codex_acp_runner(
        command=FAKE_COMMAND, working_directory=str(workspace)
    ),
    "claude": lambda workspace: claude_acp_runner(
        command=FAKE_COMMAND, working_directory=str(workspace)
    ),
}

PROFILE = AgentProfile(
    agent_id=AgentId("coder"),
    instructions="Do exactly what you are asked.",
    description="Codes.",
)


@pytest.mark.parametrize("provider", sorted(RUNNERS))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_approval_contract_holds_over_acp(
    provider: str, scenario: str, tmp_path: Path
) -> None:
    """Approve, cancel, and a session grant replayed to a new agent process."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transcript = Transcript(f"acp-{provider}-{scenario}", provider, "fake")

    async def run() -> None:
        client, chat = await open_chat(
            RUNNERS[provider](workspace),
            transcript,
            runner_name=provider,
            pause_timeout=FAKE_PAUSE_TIMEOUT,
            turn_timeout=FAKE_TURN_TIMEOUT,
        )
        try:
            await SCENARIOS[scenario](
                chat, workspace, lambda command: f"{DIRECTIVE} {command}"
            )
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    finally:
        transcript.write()


def _turn(runner: ACPAgentRunner, decide: ApprovalDecision, workspace: Path):
    asked: list[ApprovalRequest] = []
    observed: list[Message] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        asked.append(request)
        # Everything said and done before the question is published before it.
        assert [message.tool_calls for message in observed if message.tool_calls]
        return decide

    turn = asyncio.run(
        runner.run_turn_interactive(
            AgentRunId("run-1"),
            PROFILE,
            (Message.user(f"{DIRECTIVE} printf 'hi' > out.txt"),),
            approve,
            on_message=observed.append,
        )
    )
    return turn, asked, observed


def test_a_permission_request_reads_as_an_approval_of_that_command(tmp_path) -> None:
    runner = codex_acp_runner(command=FAKE_COMMAND, working_directory=str(tmp_path))

    turn, asked, observed = _turn(runner, ApprovalDecision.ACCEPT, tmp_path)

    [request] = asked
    assert request.kind is ApprovalKind.COMMAND_EXECUTION
    assert request.command == "printf 'hi' > out.txt"
    assert request.cwd == str(tmp_path)
    assert request.tool_call_id == "run-1:call_run"
    assert request.allowed_decisions == (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.ACCEPT_FOR_SESSION,
        ApprovalDecision.CANCEL,
    )
    assert runner.permission_translator.scope_for(request).capability is (
        ApprovalCapability.BASH
    )
    assert (tmp_path / "out.txt").read_text() == "hi"
    # The call, its result, then the answer -- streamed in that order, and the
    # turn's transcript is exactly what was streamed.
    assert [message.role for message in observed] == [
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert observed[0].tool_calls[0].call_id == "run-1:call_run"
    assert json.loads(observed[0].tool_calls[0].arguments) == {
        "command": "printf 'hi' > out.txt"
    }
    assert turn.transcript == tuple(observed)
    assert turn.message.content == "Ran it."


def test_a_cancelled_approval_runs_nothing_and_still_answers(tmp_path) -> None:
    runner = claude_acp_runner(command=FAKE_COMMAND, working_directory=str(tmp_path))

    turn, asked, _ = _turn(runner, ApprovalDecision.CANCEL, tmp_path)

    assert asked
    assert not (tmp_path / "out.txt").exists()
    assert turn.message.content == "Stopped, as asked."


def test_a_turn_with_nobody_to_ask_is_refused_rather_than_allowed(tmp_path) -> None:
    runner = codex_acp_runner(command=FAKE_COMMAND, working_directory=str(tmp_path))

    turn = asyncio.run(
        runner.run_turn(
            AgentRunId("run-1"),
            PROFILE,
            (Message.user(f"{DIRECTIVE} printf 'hi' > out.txt"),),
        )
    )

    assert not (tmp_path / "out.txt").exists()
    assert turn.message.content == "Stopped, as asked."


def test_the_runner_implements_every_port_the_chat_and_workflows_use() -> None:
    runner = codex_acp_runner()

    assert isinstance(runner, StreamingAgentRunner)
    assert isinstance(runner, InteractiveMcpAgentRunner)


def test_a_profile_with_tool_grants_is_refused(tmp_path) -> None:
    with pytest.raises(ACPToolsUnsupportedError):
        asyncio.run(
            codex_acp_runner(command=FAKE_COMMAND).run_turn(
                AgentRunId("run-1"),
                PROFILE,
                (Message.user("hello"),),
                tools=(ToolSpec(name="search"),),
            )
        )


# --- what each agent is configured with -------------------------------------


def test_codex_runs_under_the_strictest_codex_acp_preset() -> None:
    """codex-acp has no read-only sandbox; its `read-only` preset asks a person."""
    for sandbox in ("read-only", "workspace-write"):
        runner = codex_acp_runner(sandbox=sandbox)
        assert runner.provider.env["INITIAL_AGENT_MODE"] == "read-only"
        assert "CODEX_CONFIG" not in runner.provider.env
    assert (
        codex_acp_runner(sandbox="danger-full-access").provider.env["INITIAL_AGENT_MODE"]
        == "agent-full-access"
    )
    with pytest.raises(ValueError):
        codex_acp_runner(sandbox="anything")


def test_codex_attribution_and_model_reach_the_session() -> None:
    runner = codex_acp_runner(attribution=False, model="gpt-default")

    config = json.loads(runner.provider.env["CODEX_CONFIG"])
    assert "AI attribution" in config["developer_instructions"]
    assert runner.session_config_for(PROFILE) == {"model": "gpt-default"}
    profile = AgentProfile(
        agent_id=AgentId("coder"), instructions="", description="", model="gpt-picked"
    )
    assert runner.session_config_for(profile) == {"model": "gpt-picked"}
    assert codex_acp_runner().session_config_for(PROFILE) is None


def test_claude_is_given_its_allowed_tools_and_settings() -> None:
    runner = claude_acp_runner(
        allowed_tools=("Read", "Glob", "Grep", "Edit"),
        attribution=False,
        output_style=ResponseStyle.CONCISE,
        model="claude-picked",
    )

    config = runner.session_config_for(PROFILE)
    options = config["claudeCode"]["options"]
    assert options["allowedTools"] == ["Read", "Glob", "Grep", "Edit"]
    # Shell is never preapproved: `approvals.bash` is applied per command, at
    # the permission request.
    assert "Bash" not in options["allowedTools"]
    assert "tools" not in options
    assert options["settings"]["outputStyle"] == "Concise"
    assert options["settings"]["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}
    assert "AI attribution" in options["systemPrompt"]["append"]
    assert config["model"] == "claude-picked"
    # Whatever mode the operator's own Claude settings name, Engine is asked.
    assert config["mode"] == "default"


def test_a_read_only_claude_has_only_the_read_only_tools() -> None:
    options = claude_acp_runner(tools=READ_ONLY_TOOLS).session_config_for(PROFILE)[
        "claudeCode"
    ]["options"]

    assert options["tools"] == list(READ_ONLY_TOOLS)
    assert options["allowedTools"] == list(READ_ONLY_TOOLS)


def test_the_bound_mcp_server_is_attached_to_the_session(tmp_path) -> None:
    """In ACP's stdio shape, which requires `env` even when there is none."""
    log = tmp_path / "sent.jsonl"
    runner = ACPAgentRunner(
        codex_acp_runner(command=FAKE_COMMAND).provider.__class__(
            command=(sys.executable, str(FAKE_AGENT), "--permission"),
            env={"FAKE_AGENT_LOG": str(log)},
        ),
        working_directory=str(tmp_path),
    )
    server = McpServerConfig(name="engine", command="engine-mcp", args=("--token", "t"))

    asyncio.run(
        runner.run_turn_with_mcp(AgentRunId("run-1"), PROFILE, (Message.user("hi"),), server)
    )

    sent = [json.loads(line) for line in log.read_text().splitlines()]
    [new] = [message for message in sent if message.get("method") == "session/new"]
    assert new["params"]["mcpServers"] == [
        {"name": "engine", "command": "engine-mcp", "args": ["--token", "t"], "env": []}
    ]


def test_codex_asking_about_the_bound_server_s_tool_is_answered_for_it() -> None:
    """Codex reports an MCP call as `execute`, naming the server in `rawInput`.

    The permission request itself names nothing but the call, so what the
    stream said about that call is what identifies the runtime's own server.
    """
    from langgraph_acp import ACPEvent, ACPEventType, ACPPermissionRequest

    from engine.adapters.agent_runner.acp import _Turn

    asked: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        asked.append(request)
        return ApprovalDecision.CANCEL

    def request(server: str) -> ACPPermissionRequest:
        return ACPPermissionRequest.from_params(
            "codex",
            {
                "sessionId": "s",
                "toolCall": {"toolCallId": f"call-{server}"},
                "options": [
                    {"optionId": "approved", "kind": "allow_once"},
                    {"optionId": "abort", "kind": "reject_once"},
                ],
            },
        )

    async def run() -> list[str | None]:
        turn = _Turn(
            agent="codex",
            agent_run_id=AgentRunId("run-1"),
            working_directory="/work",
            on_message=lambda _message: None,
            on_approval=approve,
            mcp_server="planning",
        )
        answers = []
        for server in ("planning", "elsewhere"):
            await turn.observe(
                ACPEvent(
                    agent="codex",
                    type=ACPEventType.TOOL_STARTED,
                    data={
                        "toolCallId": f"call-{server}",
                        "kind": "execute",
                        "title": f"mcp.{server}.add_milestone",
                        "rawInput": {"server": server, "tool": "add_milestone"},
                    },
                )
            )
            await turn.observe(
                ACPEvent(agent="codex", type=ACPEventType.PERMISSION_REQUESTED)
            )
            answers.append((await turn.answer(request(server))).option_id)
        return answers

    assert asyncio.run(run()) == ["approved", "abort"]
    [other] = asked
    assert other.kind is ApprovalKind.TOOL_USE
    assert other.tool_name == "mcp__elsewhere__add_milestone"
