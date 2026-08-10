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

import pytest

from engine.adapters.agent_runner.codex import (
    CodexAgentRunner,
    CodexExecutionError,
    CodexToolsUnsupportedError,
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
from engine.ports import AgentRunner, FinishReason

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
    assert isinstance(CodexAgentRunner(), AgentRunner)


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


def test_a_profile_may_choose_its_model() -> None:
    profile = AgentProfile(agent_id=AgentId("coder"), instructions="", model="gpt-5.1-codex")

    argv = CodexAgentRunner().command_line(profile)

    assert argv[argv.index("--model") + 1] == "gpt-5.1-codex"


def test_chat_cannot_edit_the_tree_by_default() -> None:
    assert CodexAgentRunner().command_line(PROFILE)[-3:-2] == ["read-only"]


def test_a_nonsense_sandbox_is_caught_at_construction() -> None:
    with pytest.raises(ValueError):
        CodexAgentRunner(sandbox="yolo")
