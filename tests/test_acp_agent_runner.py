"""The ACP runner: the approval contract, and what each agent is configured with.

The scenarios are `approval_scenarios`' -- approve, cancel, and allow for the
session across a turn boundary -- run through the web app over
`ACPAgentRunner` against `langgraph-acp`'s fake agent. The fake really runs the
command it is allowed to, so they assert on the filesystem, and they need no
model, network or npm, so they block every pull request.

Both agents' runners are driven, with only their command pointed at the fake:
what differs between them is the configuration they send, and that is pinned
separately below from what reaches the agent.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from engine.adapters.agent_runner.acp import (
    CODEX_SANDBOXES,
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
from engine.adapters.agent_runner.acp.questions import (
    content_from_answers,
    questions_from_form,
)
from engine.ports import (
    ApprovalRequest,
    ApprovalResponse,
    InteractiveMcpAgentRunner,
    McpServerConfig,
    ResponseStyle,
    StreamingAgentRunner,
    UserInputAnswer,
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
)
from engine.ports.permissions import ApprovalCapability
from provider_fakes import DIRECTIVE
from approval_scenarios import (
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


# --- questions ---------------------------------------------------------------

ASKING_COMMAND = (sys.executable, str(FAKE_AGENT), "--ask")


def _ask(tmp_path: Path, answer: ApprovalResponse | None):
    """One turn in which the agent asks which colour, and what it was told."""
    runner = claude_acp_runner(command=ASKING_COMMAND, working_directory=str(tmp_path))
    asked: list[ApprovalRequest] = []

    async def respond(request: ApprovalRequest) -> ApprovalResponse:
        asked.append(request)
        assert answer is not None
        return answer

    run = (
        runner.run_turn(AgentRunId("run-1"), PROFILE, (Message.user("ask me"),))
        if answer is None
        else runner.run_turn_interactive(
            AgentRunId("run-1"), PROFILE, (Message.user("ask me"),), respond
        )
    )
    turn = asyncio.run(run)
    written = tmp_path / "answer.json"
    return turn, asked, json.loads(written.read_text()) if written.exists() else None


def test_an_agent_s_question_reaches_the_user_and_their_answer_the_agent(
    tmp_path,
) -> None:
    turn, asked, answered = _ask(
        tmp_path,
        UserInputResponse(answers=(UserInputAnswer("question_0", ("Red",)),)),
    )

    [request] = asked
    assert request.kind is ApprovalKind.USER_INPUT
    assert request.requires_human
    assert request.tool_name == "AskUserQuestion"
    assert request.tool_call_id == "run-1:call_ask"
    assert request.allowed_decisions == (ApprovalDecision.CANCEL,)
    # The free-text field is the question's "other" answer, not a question.
    assert request.questions == (
        UserInputQuestion(
            question_id="question_0",
            header="Colour",
            question="Which colour?",
            options=(UserInputOption("Red", "Warm"), UserInputOption("Blue")),
            allows_other=True,
        ),
    )
    assert answered == {"action": "accept", "content": {"question_0": "Red"}}
    assert turn.message.content == "Answered: accept."


def test_an_answer_none_of_the_options_cover_is_the_other_answer(tmp_path) -> None:
    _, _, answered = _ask(
        tmp_path,
        UserInputResponse(answers=(UserInputAnswer("question_0", ("Green",)),)),
    )

    assert answered == {"action": "accept", "content": {"question_0_custom": "Green"}}


def test_a_question_the_user_cancels_is_cancelled(tmp_path) -> None:
    _, _, answered = _ask(tmp_path, ApprovalDecision.CANCEL)

    assert answered == {"action": "cancel"}


def test_a_turn_with_nobody_to_ask_is_not_asked(tmp_path) -> None:
    turn, _, answered = _ask(tmp_path, None)

    assert answered is None
    assert turn.message.content == "Nobody to ask."


def test_codex_s_questions_read_the_same_as_claude_s() -> None:
    """codex-acp titles a field with the question and describes it with the header."""
    schema = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "title": "Which files?",
                "description": "Scope",
                "_meta": {"codex": {"isOther": True, "isSecret": False}},
                "oneOf": [
                    {"const": "All", "title": "All"},
                    {"const": "None of the above", "title": "None of the above"},
                ],
            },
            "scope_note": {
                "type": "string",
                "_meta": {"codex": {"questionId": "scope", "role": "user_note"}},
            },
        },
        "required": ["scope"],
    }

    [question] = questions_from_form("Codex needs your input to continue.", schema)

    assert (question.question_id, question.header, question.question) == (
        "scope",
        "Scope",
        "Which files?",
    )
    assert [option.label for option in question.options] == ["All", "None of the above"]
    assert content_from_answers(
        schema,
        UserInputResponse(answers=(UserInputAnswer("scope", ("All", "src only")),)),
    ) == {"scope": "All", "scope_note": "src only"}


# --- what each agent is configured with -------------------------------------


@pytest.mark.parametrize("sandbox", CODEX_SANDBOXES)
def test_codex_runs_in_the_sandbox_engine_names(sandbox: str) -> None:
    """codex-acp is handed a Codex that holds every turn to `sandbox`."""
    runner = codex_acp_runner(sandbox=sandbox)
    env = runner.provider.env

    # A person reviews, never a model on their behalf.
    assert env["INITIAL_AGENT_MODE"] == "read-only"
    assert env["ENGINE_CODEX_SANDBOX"] == sandbox
    assert os.access(env["CODEX_PATH"], os.X_OK)
    assert "ENGINE_CODEX_PATH" not in env
    assert "CODEX_CONFIG" not in env


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_codex_launcher_is_private_to_this_user() -> None:
    """Not at a shared, guessable path another user could have claimed first."""
    launcher = Path(codex_acp_runner().provider.env["CODEX_PATH"])

    assert launcher.parent != Path(tempfile.gettempdir()) / "engine-codex-policy"
    assert not launcher.is_symlink()
    for path in (launcher, launcher.parent):
        info = path.lstat()
        assert info.st_uid == os.getuid()
        assert info.st_mode & 0o077 == 0


def test_an_unknown_codex_sandbox_is_refused() -> None:
    with pytest.raises(ValueError):
        codex_acp_runner(sandbox="anything")


def test_the_operator_s_codex_still_runs_under_the_sandbox() -> None:
    env = codex_acp_runner(env={"CODEX_PATH": "/opt/codex"}).provider.env

    assert env["ENGINE_CODEX_PATH"] == "/opt/codex"
    assert env["CODEX_PATH"] != "/opt/codex"


@pytest.mark.skipif(os.name == "nt", reason="the fake Codex is a shell script")
@pytest.mark.parametrize(
    ("sandbox", "policy"),
    [
        ("read-only", {"type": "readOnly"}),
        ("workspace-write", {"type": "workspaceWrite"}),
        ("danger-full-access", {"type": "dangerFullAccess"}),
    ],
)
def test_every_codex_turn_is_pinned_to_the_sandbox_and_asks(
    sandbox: str, policy: dict[str, str], tmp_path: Path
) -> None:
    """What codex-acp sends is rewritten before Codex sees it.

    Its `read-only` preset writes anywhere in the worktree unasked and its
    full-access preset never asks; neither reaches Codex. Driven through the
    launcher codex-acp is given, with a Codex that writes down what it received.
    """
    received = tmp_path / "received.jsonl"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > {received}.argv\n'
        f"exec cat >> {received}\n"
    )
    fake_codex.chmod(0o755)
    runner = codex_acp_runner(sandbox=sandbox, env={"CODEX_PATH": str(fake_codex)})
    preset = {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": []},
    }
    sent = [
        {"id": 1, "method": "thread/start", "params": {"cwd": str(tmp_path)}},
        {"id": 2, "method": "turn/start", "params": {"threadId": "t", **preset}},
    ]

    done = subprocess.run(
        [runner.provider.env["CODEX_PATH"], "app-server"],
        input="".join(json.dumps(message) + "\n" for message in sent),
        env={**os.environ, **runner.provider.env},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert Path(f"{received}.argv").read_text().strip() == "app-server"
    thread_start, turn_start = map(json.loads, received.read_text().splitlines())
    assert thread_start == sent[0]
    assert turn_start["params"] == {
        "threadId": "t",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": policy,
    }


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
