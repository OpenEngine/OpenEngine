"""The Codex adapter, tested without spawning Codex.

Everything that turns a conversation into a prompt and an event stream back into
a turn is a pure function, so the interesting behaviour is testable at speed.
What is left for the subprocess -- argv, exit codes, timeouts -- is thin by
design.

The JSONL below is a real transcript, captured from `codex exec --json`. Invented
fixtures for a wire format are worth very little: they test the parser against
the shape you assumed rather than the one the CLI emits.
"""

import asyncio
import json
import textwrap

import pytest

from engine.adapters.agent_runner.codex import (
    CODEX_PERMISSION_TRANSLATOR,
    CodexAgentRunner,
    CodexExecutionError,
    CodexToolsUnsupportedError,
    _app_server_thread_params,
    approval_request_from_app_server,
    app_server_sandbox_policy,
    parse_events,
    render_prompt,
    thread_id_of,
    turn_from_events,
)
from engine.domain import (
    AgentId,
    AgentProfile,
    AgentRunId,
    Message,
    Role,
    ToolSpec,
    WorkspaceId,
)
from engine.ports import (
    AgentRunner,
    ApprovalCapability,
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    FinishReason,
    InteractiveAgentRunner,
    InteractiveMcpAgentRunner,
    McpAgentRunner,
    McpServerConfig,
    StreamingMcpAgentRunner,
    PermissionScope,
    PermissionTranslator,
)

#: Captured from `codex exec --json --sandbox read-only "Reply with exactly the
#: word: pong"` against codex-cli 0.144.4.
REAL_TRANSCRIPT = """\
{"type":"thread.started","thread_id":"019fed6b-1b81-7500-a93d-bd1f2143f03e"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"pong"}}
{"type":"turn.completed","usage":{"input_tokens":15276,"cached_input_tokens":9984,\
"output_tokens":5,"reasoning_output_tokens":0}}
"""

#: Also captured: `codex exec --json "Run ls on the packages directory, then
#: reply DONE."` -- narration, a command, its output, then the answer.
REAL_TOOL_TRANSCRIPT = """\
{"type":"thread.started","thread_id":"019fed9b-6bdb-7c40-a73a-9f5a70812a10"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message",\
"text":"I\\u2019ll inspect the `packages` directory now."}}
{"type":"item.started","item":{"id":"item_1","type":"command_execution",\
"command":"/bin/zsh -lc 'ls packages'","aggregated_output":"","exit_code":null,\
"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution",\
"command":"/bin/zsh -lc 'ls packages'",\
"aggregated_output":"adapters\\ndomain\\nengine\\nports\\nruntime\\n","exit_code":0,\
"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"DONE"}}
{"type":"turn.completed","usage":{"input_tokens":30527,"cached_input_tokens":19968,\
"output_tokens":74,"reasoning_output_tokens":0}}
"""

PROFILE = AgentProfile(
    agent_id=AgentId("coder"),
    instructions="You are terse.",
    description="Reads code.",
)


def test_runner_satisfies_the_port() -> None:
    runner = CodexAgentRunner()

    assert isinstance(runner, AgentRunner)
    assert isinstance(runner, InteractiveAgentRunner)
    assert isinstance(runner.permission_translator, PermissionTranslator)


def test_attribution_can_be_disabled_for_both_codex_transports() -> None:
    runner = CodexAgentRunner(attribution=False)

    for argv in (runner.command_line(PROFILE), runner.app_server_command_line()):
        instruction = argv[argv.index("-c") + 1]
        assert instruction.startswith("developer_instructions=")
        assert "Co-authored-by" in instruction


@pytest.mark.parametrize(
    ("kind", "command", "expected"),
    [
        (
            ApprovalKind.COMMAND_EXECUTION,
            "uv run pytest",
            PermissionScope(ApprovalCapability.BASH, "uv run pytest"),
        ),
        (ApprovalKind.FILE_CHANGE, None, PermissionScope(ApprovalCapability.EDIT)),
        (ApprovalKind.TOOL_USE, None, None),
    ],
)
def test_permission_translator_maps_codex_requests_to_engine_capabilities(
    kind: ApprovalKind,
    command: str | None,
    expected: PermissionScope | None,
) -> None:
    request = ApprovalRequest(
        approval_id="provider-approval",
        kind=kind,
        command=command,
    )

    assert CODEX_PERMISSION_TRANSLATOR.scope_for(request) == expected


# --- parsing ----------------------------------------------------------------


def test_parses_a_real_transcript() -> None:
    turn = turn_from_events(parse_events(REAL_TRANSCRIPT))

    assert turn.message.content == "pong"
    assert turn.finish_reason is FinishReason.STOP
    assert turn.steps == ()
    assert turn.usage is not None
    assert (turn.usage.prompt_tokens, turn.usage.completion_tokens) == (15276, 5)


def test_cached_tokens_are_reported() -> None:
    """The number that says whether a prompt is being re-billed in full."""
    usage = turn_from_events(parse_events(REAL_TRANSCRIPT)).usage

    assert usage.cached_prompt_tokens == 9984
    assert usage.uncached_prompt_tokens == 15276 - 9984


def test_reads_the_session_id_for_later() -> None:
    assert thread_id_of(parse_events(REAL_TRANSCRIPT)) == "019fed6b-1b81-7500-a93d-bd1f2143f03e"


def test_survives_noise_around_the_event_stream() -> None:
    """The CLI writes warnings and progress in with the JSONL; losing an answer
    that arrived because of a stray line would be the worst kind of flake."""
    noisy = (
        "Reading additional input from stdin...\n"
        "2026-08-10T20:44:06Z ERROR codex_models_manager::cache: failed to load\n"
        + REAL_TRANSCRIPT
        + "{ this line is not json }\n"
    )

    assert turn_from_events(parse_events(noisy)).message.content == "pong"


def test_narration_is_a_step_and_the_last_message_is_the_answer() -> None:
    """Codex narrates before it acts. Concatenating the narration onto the answer
    reads as one confused message; it is a step."""
    stream = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"I will look"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"the answer"}}\n'
        '{"type":"turn.completed","usage":{}}\n'
    )

    turn = turn_from_events(parse_events(stream))

    assert turn.message.content == "the answer"
    assert [step.content for step in turn.steps] == ["I will look"]


def test_what_the_agent_did_is_recorded_not_just_what_it_concluded() -> None:
    """The whole point of steps: the command and its output survive into the
    conversation, so a later reader can see why the answer is what it is."""
    turn = turn_from_events(parse_events(REAL_TOOL_TRANSCRIPT))

    assert turn.message.content == "DONE"
    narration, call, result = turn.steps
    assert narration.content == "I’ll inspect the `packages` directory now."
    assert call.tool_calls[0].name == "command_execution"
    assert "ls packages" in call.tool_calls[0].arguments
    assert result.role is Role.TOOL
    assert "adapters\ndomain" in result.content
    assert "(exit 0)" in result.content


def test_a_call_and_its_result_are_tied_together() -> None:
    """Paired by id, and the id carries the thread so two turns cannot collide."""
    turn = turn_from_events(parse_events(REAL_TOOL_TRANSCRIPT))
    _, call, result = turn.steps

    assert call.tool_calls[0].call_id == result.tool_call_id
    assert call.tool_calls[0].call_id.startswith("019fed9b-")


def test_recorded_actions_do_not_ask_the_caller_to_run_anything() -> None:
    """The distinction that matters: these already happened. Reporting them as
    tool calls on the answer would send the caller round the loop again."""
    turn = turn_from_events(parse_events(REAL_TOOL_TRANSCRIPT))

    assert not turn.wants_tools
    assert turn.tool_calls == ()
    assert turn.transcript == (*turn.steps, turn.message)


def test_an_unknown_item_type_is_recorded_rather_than_dropped() -> None:
    """A Codex release that adds an item type should not silently punch a hole
    in the audit trail."""
    stream = (
        '{"type":"item.completed","item":{"id":"i1","type":"quantum_refactor",'
        '"target":"main.py","result":"reticulated"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )

    call, result = turn_from_events(parse_events(stream)).steps

    assert call.tool_calls[0].name == "quantum_refactor"
    assert "main.py" in call.tool_calls[0].arguments
    assert result.content == "reticulated"


def test_stripped_reasoning_is_not_recorded() -> None:
    """Codex sends reasoning items with the content removed. Half a thought is
    worse in a transcript than none."""
    stream = (
        '{"type":"item.completed","item":{"id":"i1","type":"reasoning","text":""}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )

    assert turn_from_events(parse_events(stream)).steps == ()


def test_a_failed_turn_is_reported_not_raised() -> None:
    """A model that failed mid-answer still said something worth showing."""
    stream = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"I hit a wall"}}\n'
        '{"type":"turn.failed","error":{"message":"stream disconnected"}}\n'
    )

    turn = turn_from_events(parse_events(stream))

    assert turn.finish_reason is FinishReason.ERROR
    assert turn.message.content == "I hit a wall"


def test_a_failure_with_nothing_said_still_says_why() -> None:
    """The placeholder is all there is to read, so it carries the reason."""
    turn = turn_from_events(
        parse_events('{"type":"turn.failed","error":{"message":"stream disconnected"}}')
    )

    assert turn.finish_reason is FinishReason.ERROR
    assert "stream disconnected" in turn.message.content


def test_no_answer_at_all_is_an_error() -> None:
    with pytest.raises(CodexExecutionError):
        turn_from_events(parse_events('{"type":"turn.completed","usage":{}}'))


# --- prompt rendering -------------------------------------------------------


def test_the_first_message_carries_the_instructions() -> None:
    prompt = render_prompt(PROFILE, (Message.user("hello"),))

    assert "You are terse." in prompt
    assert prompt.endswith("User: hello")


def test_history_is_labelled_by_role() -> None:
    """Codex takes one block of text, so the roles a chat API carries
    structurally have to be spelled out."""
    prompt = render_prompt(
        PROFILE,
        (Message.user("what is 2+2"), Message.assistant("4"), Message.user("and times 3")),
    )

    assert "User: what is 2+2" in prompt
    assert "Assistant: 4" in prompt
    assert prompt.endswith("User: and times 3")


def test_each_turn_extends_the_previous_prompt_rather_than_rewriting_it() -> None:
    """The precondition for a prompt-cache hit. An earlier version moved the
    latest message between two headings every turn, which broke the shared
    prefix one line after the instructions."""
    first = (Message.user("what is 2+2"),)
    second = (*first, Message.assistant("4"), Message.user("and times 3"))
    third = (*second, Message.assistant("12"), Message.user("thanks"))

    prompts = [render_prompt(PROFILE, messages) for messages in (first, second, third)]

    assert prompts[1].startswith(prompts[0])
    assert prompts[2].startswith(prompts[1])


def test_recorded_actions_replay_into_later_prompts() -> None:
    """A stateless runner starts each turn cold, so what the agent already did
    has to come back in through the transcript or it is forgotten."""
    steps = turn_from_events(parse_events(REAL_TOOL_TRANSCRIPT)).steps

    prompt = render_prompt(PROFILE, (Message.user("what is in packages?"), *steps))

    assert "ran command_execution" in prompt
    assert "ls packages" in prompt
    assert "Tool result: adapters" in prompt


def test_a_huge_output_is_truncated_on_replay_but_kept_whole() -> None:
    """Storage is complete; what a later turn re-reads is bounded, because an
    8KB dump from three questions ago is re-billed on every turn after it."""
    dump = Message.tool_result("call-1", "x" * 5000)

    prompt = render_prompt(PROFILE, (Message.user("read it"), dump))

    assert len(prompt) < 3000
    assert "more characters, stored in full" in prompt
    assert dump.content == "x" * 5000, "the message itself must not be edited"


def test_rendering_an_empty_conversation_is_refused() -> None:
    with pytest.raises(ValueError):
        render_prompt(PROFILE, ())


# --- what this runner cannot do, said out loud ------------------------------


def test_granted_tools_are_refused_rather_than_dropped() -> None:
    """Codex runs its own tools. Ignoring ours would leave an agent quietly less
    capable than its profile promises, with no way for the caller to notice."""
    runner = CodexAgentRunner()

    with pytest.raises(CodexToolsUnsupportedError) as raised:
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
    runner = CodexAgentRunner()

    with pytest.raises(NotImplementedError):
        asyncio.run(
            runner.run_turn(
                AgentRunId("ar-1"),
                PROFILE,
                (Message.user("go"),),
                workspace_id=WorkspaceId("ws-1"),
            )
        )


# --- invocation -------------------------------------------------------------


def test_the_command_line_is_inspectable_without_running_anything() -> None:
    argv = CodexAgentRunner(sandbox="read-only", working_directory="/tmp/x").command_line(PROFILE)

    assert argv[:3] == ["codex", "exec", "--json"]
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "-C" in argv and argv[argv.index("-C") + 1] == "/tmp/x"
    assert "--model" not in argv


def test_terminal_mcp_configuration_is_passed_to_codex() -> None:
    server = McpServerConfig("workflow", "/usr/bin/python3", ("-m", "terminal"))
    argv = CodexAgentRunner().command_line(PROFILE, mcp_server=server)

    assert isinstance(CodexAgentRunner(), McpAgentRunner)
    assert isinstance(CodexAgentRunner(), StreamingMcpAgentRunner)
    assert isinstance(CodexAgentRunner(), InteractiveMcpAgentRunner)
    assert 'mcp_servers.workflow.command="/usr/bin/python3"' in argv
    assert 'mcp_servers.workflow.args=["-m", "terminal"]' in argv
    assert _app_server_thread_params("/workspace", "", server)["config"] == {
        "mcp_servers": {
            "workflow": {
                "command": "/usr/bin/python3",
                "args": ["-m", "terminal"],
            }
        }
    }


def test_a_profile_may_choose_its_model() -> None:
    profile = AgentProfile(agent_id=AgentId("coder"), instructions="", model="gpt-5.1-codex")

    argv = CodexAgentRunner().command_line(profile)

    assert argv[argv.index("--model") + 1] == "gpt-5.1-codex"


def test_chat_cannot_edit_the_tree_by_default() -> None:
    assert CodexAgentRunner().command_line(PROFILE)[-3:-2] == ["read-only"]


def test_a_nonsense_sandbox_is_caught_at_construction() -> None:
    with pytest.raises(ValueError):
        CodexAgentRunner(sandbox="yolo")


# --- interactive app-server protocol ---------------------------------------


def test_app_server_uses_the_v2_sandbox_shapes() -> None:
    assert app_server_sandbox_policy("read-only") == {"type": "readOnly"}
    assert app_server_sandbox_policy("workspace-write") == {"type": "workspaceWrite"}
    assert app_server_sandbox_policy("danger-full-access") == {"type": "dangerFullAccess"}


def test_app_server_approval_exposes_only_the_three_engine_decisions() -> None:
    request = approval_request_from_app_server(
        {
            "id": 17,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "reason": "needs write access",
                "command": "touch output.txt",
                "cwd": "/workspace",
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
            },
        }
    )

    assert request is not None
    assert request.kind is ApprovalKind.COMMAND_EXECUTION
    assert request.command == "touch output.txt"
    assert request.allowed_decisions == (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.ACCEPT_FOR_SESSION,
        ApprovalDecision.CANCEL,
    )


def test_app_server_approval_without_an_item_names_no_call() -> None:
    """Nothing to pair it with, and nothing invented: it belongs to the turn."""
    request = approval_request_from_app_server(
        {
            "id": 18,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "command": "ls"},
        }
    )

    assert request is not None
    assert request.tool_call_id is None


def _fake_app_server(tmp_path) -> str:
    binary = tmp_path / "codex"
    binary.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import json
            import sys

            def receive():
                return json.loads(sys.stdin.readline())

            def send(message):
                print(json.dumps(message), flush=True)

            initialize = receive()
            send({"id": initialize["id"], "result": {"userAgent": "fake"}})
            assert receive()["method"] == "initialized"
            start = receive()
            send({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
            turn = receive()
            send({"id": turn["id"], "result": {"turn": {"id": "turn-1"}}})
            send({"method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {"id": "cmd-1", "type": "commandExecution",
                         "command": "touch output.txt", "cwd": ".",
                         "commandActions": [], "status": "inProgress"}}})
            send({"id": "approval-1", "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": "thread-1", "turnId": "turn-1",
                             "itemId": "cmd-1", "command": "touch output.txt",
                             "cwd": ".", "availableDecisions":
                             ["accept", "acceptForSession", "decline", "cancel"]}})
            decision = receive()["result"]["decision"]
            send({"method": "item/completed", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 1,
                "item": {"id": "cmd-1", "type": "commandExecution",
                         "command": "touch output.txt", "cwd": ".",
                         "commandActions": [], "status": "completed", "exitCode": 0,
                         "aggregatedOutput": decision}}})
            send({"method": "item/completed", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 2,
                "item": {"id": "msg-1", "type": "agentMessage", "text": "done"}}})
            send({"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                    "last": {"inputTokens": 10, "cachedInputTokens": 4,
                             "outputTokens": 2, "reasoningOutputTokens": 0,
                             "totalTokens": 12},
                    "total": {"inputTokens": 10, "cachedInputTokens": 4,
                              "outputTokens": 2, "reasoningOutputTokens": 0,
                              "totalTokens": 12}}}})
            send({"method": "turn/completed", "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "items": [], "status": "completed"}}})
            '''
        )
    )
    binary.chmod(0o755)
    return str(binary)


def test_interactive_turn_round_trips_an_app_server_approval(tmp_path) -> None:
    runner = CodexAgentRunner(binary_path=_fake_app_server(tmp_path))
    observed: list[Message] = []
    approvals = []

    async def approve(request):
        approvals.append(request)
        return ApprovalDecision.ACCEPT_FOR_SESSION

    turn = asyncio.run(
        runner.run_turn_interactive(
            AgentRunId("ar-1"),
            PROFILE,
            (Message.user("create it"),),
            approve,
            on_message=observed.append,
        )
    )

    assert [request.command for request in approvals] == ["touch output.txt"]
    # The pause names the call this turn recorded, spelled the way the
    # transcript spells it. That identity is what lets a reader see the request
    # beside the command it was about rather than at the end of the turn.
    assert (
        approvals[0].tool_call_id
        == turn.steps[0].tool_calls[0].call_id
        == "thread-1:cmd-1"
    )
    assert turn.message.content == "done"
    assert turn.steps[1].content == "acceptForSession\n(exit 0)"
    assert turn.usage is not None and turn.usage.cached_prompt_tokens == 4
    assert observed == list(turn.transcript)
    assert runner.app_server_command_line()[1:] == ["app-server"]


# --- how long a turn may take -----------------------------------------------
#
# The only tests here that spawn anything. A fake `codex` is enough: what is
# under test is the deadline, not the CLI.


def _fake_codex(tmp_path, body: str) -> str:
    """An executable standing in for the CLI. `body` is shell."""
    binary = tmp_path / "codex"
    binary.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}\n")
    binary.chmod(0o755)
    return str(binary)


def test_completed_messages_stream_in_transcript_order(tmp_path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(REAL_TOOL_TRANSCRIPT)
    runner = CodexAgentRunner(binary_path=_fake_codex(tmp_path, f"cat {transcript}"))
    observed: list[Message] = []

    turn = asyncio.run(
        runner.run_turn_streamed(
            AgentRunId("ar-1"), PROFILE, (Message.user("go"),), observed.append
        )
    )

    assert observed == list(turn.transcript)


def test_a_jsonl_event_may_exceed_the_stream_reader_line_limit(tmp_path) -> None:
    """Tool output lives inside one JSONL event and can easily exceed 64 KiB."""
    output = "x" * (70 * 1024)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"item.completed","item":{"id":"item_0",'
        '"type":"command_execution","aggregated_output":'
        + json.dumps(output)
        + '}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )
    runner = CodexAgentRunner(binary_path=_fake_codex(tmp_path, f"cat {transcript}"))

    turn = asyncio.run(
        runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("go"),))
    )

    assert turn.steps[1].content == output
    assert turn.message.content == "done"


def test_a_turn_is_given_no_deadline_by_default(tmp_path, monkeypatch) -> None:
    """A wall clock is the wrong thing to cut an agent off with: a long turn is
    usually a large task rather than a stuck one, and killing it throws away
    every tool call it had already made. `cancel` ends a run early instead,
    because that is a decision with someone behind it."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(REAL_TRANSCRIPT)
    deadlines: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    runner = CodexAgentRunner(binary_path=_fake_codex(tmp_path, f"cat {transcript}"))

    turn = asyncio.run(runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("ping"),)))

    assert deadlines == [None]
    assert turn.message.content == "pong"


def test_a_failing_run_reports_what_codex_said_not_what_it_warned_about(tmp_path) -> None:
    """Codex explains a failure on stdout and keeps warning on stderr regardless.

    Both lines here are verbatim from `codex exec` 0.144.4 against an account
    that is out of quota. Quoting the tail of stderr sends whoever reads the
    error after a stale cache file, when what actually happened is that the
    account cannot run a turn until Thursday.
    """
    limit = "You've hit your usage limit. Try again at Aug 20th, 2026 8:46 AM."
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "thread.started", "thread_id": "01a0"})
        + "\n"
        + json.dumps({"type": "error", "message": limit})
        + "\n"
        + json.dumps({"type": "turn.failed", "error": {"message": limit}})
        + "\n"
    )
    runner = CodexAgentRunner(
        binary_path=_fake_codex(
            tmp_path,
            f"cat {transcript}\n"
            "echo 'ERROR codex_models_manager::cache: failed to load models cache: "
            "missing field supports_reasoning_summaries' >&2\n"
            "exit 1",
        )
    )

    with pytest.raises(CodexExecutionError) as failure:
        asyncio.run(runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("hi"),)))

    assert "exited 1" in str(failure.value)
    assert "usage limit" in str(failure.value)
    assert "models cache" not in str(failure.value)


def test_a_deployment_that_wants_a_ceiling_can_still_set_one(tmp_path) -> None:
    """The knob stays, and still kills the process it gave up on."""
    runner = CodexAgentRunner(binary_path=_fake_codex(tmp_path, "sleep 30"), timeout_seconds=0.2)

    with pytest.raises(CodexExecutionError, match="did not finish"):
        asyncio.run(runner.run_turn(AgentRunId("ar-1"), PROFILE, (Message.user("ping"),)))
