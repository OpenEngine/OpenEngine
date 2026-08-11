"""The Claude Code adapter, tested without spawning Claude.

Same shape as `test_codex_adapter.py`, and the transcript below is likewise
captured from a real run rather than invented -- a fixture of the wire format
you assumed only tests your assumption.
"""

import asyncio

import pytest

import engine.adapters.agent_runner.claude_code as claude_module

from engine.adapters.agent_runner.claude_code import (
    ClaudeCodeAgentRunner,
    ClaudeExecutionError,
    ClaudeToolsUnsupportedError,
    parse_events,
    session_id_of,
    turn_from_events,
)
from engine.domain import AgentId, AgentProfile, AgentRunId, Message, Role, ToolSpec, WorkspaceId
from engine.ports import AgentRunner, FinishReason, StreamingAgentRunner

#: Captured from `claude -p --output-format stream-json --verbose --allowedTools
#: Glob Read "List the directory names under packages/adapters, then reply
#: DONE."` against Claude Code 2.1.226. Trimmed to the fields this adapter reads.
REAL_TRANSCRIPT = """\
{"type":"system","subtype":"init","cwd":"/Users/shea/code/engine",\
"session_id":"4aeecd85-e23a-48c9-87e7-938cee476896","tools":["Task","Bash","Read"]}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}
{"type":"assistant","message":{"model":"claude-opus-5","role":"assistant","content":[\
{"type":"thinking","thinking":"the user wants a listing"},\
{"type":"tool_use","id":"toolu_01WaFnohBitTsZLxaBuNg8XH","name":"Glob",\
"input":{"pattern":"packages/adapters/*"}}]}}
{"type":"user","message":{"role":"user","content":[{"tool_use_id":\
"toolu_01WaFnohBitTsZLxaBuNg8XH","type":"tool_result",\
"content":"/Users/shea/code/engine/packages/adapters/agent_runner/\\n\
/Users/shea/code/engine/packages/adapters/communications/"}]}}
{"type":"assistant","message":{"model":"claude-opus-5","role":"assistant","content":[\
{"type":"text","text":"- agent_runner\\n- communications"}]}}
{"type":"result","subtype":"success","is_error":false,"num_turns":2,\
"session_id":"4aeecd85-e23a-48c9-87e7-938cee476896","result":"- agent_runner\\n- communications",\
"usage":{"input_tokens":4,"cache_creation_input_tokens":6478,\
"cache_read_input_tokens":41156,"output_tokens":149},"total_cost_usd":0.0897}
"""

PROFILE = AgentProfile(
    agent_id=AgentId("coder"), instructions="You are terse.", description="Reads code."
)


def test_runner_satisfies_the_port() -> None:
    assert isinstance(ClaudeCodeAgentRunner(), AgentRunner)
    assert isinstance(ClaudeCodeAgentRunner(), StreamingAgentRunner)


# --- parsing ----------------------------------------------------------------


def test_parses_a_real_transcript() -> None:
    turn = turn_from_events(parse_events(REAL_TRANSCRIPT))

    assert turn.message.content == "- agent_runner\n- communications"
    assert turn.finish_reason is FinishReason.STOP


def test_tool_use_and_its_result_are_paired_by_id() -> None:
    """Claude reports tool calls structurally, so pairing is reading a field
    rather than inferring from order."""
    call, result = turn_from_events(parse_events(REAL_TRANSCRIPT)).steps

    assert call.tool_calls[0].name == "Glob"
    assert "packages/adapters/*" in call.tool_calls[0].arguments
    assert result.role is Role.TOOL
    assert result.tool_call_id == call.tool_calls[0].call_id == "toolu_01WaFnohBitTsZLxaBuNg8XH"
    assert "agent_runner" in result.content


def test_thinking_blocks_are_not_recorded() -> None:
    """The model's working, not the conversation's."""
    steps = turn_from_events(parse_events(REAL_TRANSCRIPT)).steps

    assert not any("the user wants a listing" in step.content for step in steps)


def test_fresh_written_and_read_input_are_summed() -> None:
    """Claude reports three kinds of input token. `prompt_tokens` has to mean
    the same thing it does for every other runner: all of them."""
    usage = turn_from_events(parse_events(REAL_TRANSCRIPT)).usage

    assert usage.prompt_tokens == 4 + 6478 + 41156
    assert usage.cached_prompt_tokens == 41156
    assert usage.completion_tokens == 149
    assert usage.cost_usd == pytest.approx(0.0897)


def test_recorded_actions_do_not_ask_the_caller_to_run_anything() -> None:
    turn = turn_from_events(parse_events(REAL_TRANSCRIPT))

    assert not turn.wants_tools
    assert turn.transcript == (*turn.steps, turn.message)


def test_reads_the_session_id_for_later() -> None:
    assert session_id_of(parse_events(REAL_TRANSCRIPT)) == "4aeecd85-e23a-48c9-87e7-938cee476896"


def test_a_tool_result_may_arrive_as_blocks_rather_than_a_string() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Read","input":{}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"content":[{"type":"text","text":"file body"}]}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"read it"}]}}\n'
    )

    _, result = turn_from_events(parse_events(stream)).steps

    assert result.content == "file body"


def test_a_failed_tool_result_says_so() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"Read","input":{}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"is_error":true,"content":"no such file"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"it is missing"}]}}\n'
    )

    _, result = turn_from_events(parse_events(stream)).steps

    assert result.content == "error: no such file"


def test_an_errored_run_is_reported_not_raised() -> None:
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"partway"}]}}\n'
        '{"type":"result","subtype":"error_max_turns","is_error":true,'
        '"result":"hit the turn limit","usage":{}}\n'
    )

    turn = turn_from_events(parse_events(stream))

    assert turn.finish_reason is FinishReason.ERROR
    assert turn.message.content == "partway"


def test_the_result_record_answers_when_nothing_else_did() -> None:
    """A turn that ends in a tool loop still reports something."""
    stream = '{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{}}'

    assert turn_from_events(parse_events(stream)).message.content == "done"


def test_no_answer_at_all_is_an_error() -> None:
    with pytest.raises(ClaudeExecutionError):
        turn_from_events(parse_events('{"type":"system","subtype":"init"}'))


# --- what this runner cannot do, said out loud ------------------------------


def test_granted_tools_are_refused_rather_than_dropped() -> None:
    runner = ClaudeCodeAgentRunner()

    with pytest.raises(ClaudeToolsUnsupportedError) as raised:
        asyncio.run(
            runner.run_turn(
                AgentRunId("ar-1"),
                PROFILE,
                (Message.user("go"),),
                tools=(ToolSpec(name="dispatch"),),
            )
        )

    assert raised.value.tool_names == ("dispatch",)


def test_a_workspace_it_cannot_resolve_is_refused() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(
            ClaudeCodeAgentRunner().run_turn(
                AgentRunId("ar-1"),
                PROFILE,
                (Message.user("go"),),
                workspace_id=WorkspaceId("ws-1"),
            )
        )


# --- invocation -------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeReader:
    def __init__(self, text: str = "") -> None:
        self._lines = [line.encode() for line in text.splitlines(keepends=True)]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self, stdout: str) -> None:
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader(stdout)
        self.stderr = _FakeReader()
        self.returncode: int | None = None
        self.waited = False

    async def wait(self) -> int:
        self.waited = True
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15


def test_messages_stream_before_the_process_finishes(monkeypatch) -> None:
    process = _FakeProcess(REAL_TRANSCRIPT)

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(claude_module.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(claude_module.asyncio, "create_subprocess_exec", create_process)
    seen: list[tuple[Message, bool]] = []

    async def capture(message: Message) -> None:
        seen.append((message, process.waited))

    turn = asyncio.run(
        ClaudeCodeAgentRunner().run_turn_stream(
            AgentRunId("ar-1"), PROFILE, (Message.user("inspect"),), capture
        )
    )

    assert [message for message, _ in seen] == list(turn.transcript)
    assert all(not waited for _, waited in seen)
    assert process.waited
    assert b"inspect" in process.stdin.written


def test_instructions_go_to_the_system_prompt_not_the_conversation() -> None:
    """Unlike `codex exec`, this CLI has a channel for them."""
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)

    assert argv[argv.index("--append-system-prompt") + 1] == "You are terse."


def test_chat_gets_read_only_tools_by_default() -> None:
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)
    allowed = argv[argv.index("--allowedTools") + 1 :]

    assert allowed == ["Read", "Glob", "Grep"]
    assert "Bash" not in allowed and "Edit" not in allowed
    assert "--dangerously-skip-permissions" not in argv


def test_an_empty_tool_list_omits_the_flag() -> None:
    """`--allowedTools` is variadic: passed with nothing after it, it swallows
    whatever flag comes next."""
    argv = ClaudeCodeAgentRunner(allowed_tools=()).command_line(PROFILE)

    assert "--allowedTools" not in argv


def test_a_profile_may_choose_its_model() -> None:
    profile = AgentProfile(agent_id=AgentId("coder"), instructions="", model="claude-opus-5")

    argv = ClaudeCodeAgentRunner().command_line(profile)

    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert "--append-system-prompt" not in argv, "no instructions, no flag"


def test_stream_json_needs_verbose() -> None:
    """The CLI rejects the combination without it, and the failure is opaque."""
    argv = ClaudeCodeAgentRunner().command_line(PROFILE)

    assert argv[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
