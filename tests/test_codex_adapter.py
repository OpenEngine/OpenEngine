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
from engine.domain import AgentId, AgentProfile, AgentRunId, Message, ToolSpec, WorkspaceId
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
    assert turn.usage is not None
    assert (turn.usage.prompt_tokens, turn.usage.completion_tokens) == (15276, 5)


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


def test_several_messages_become_one_answer() -> None:
    stream = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}\n'
        '{"type":"turn.completed","usage":{}}\n'
    )

    assert turn_from_events(parse_events(stream)).message.content == "first\n\nsecond"


def test_non_message_items_are_ignored() -> None:
    """Codex reports its own reasoning and commands as items too; they are its
    business, not the conversation's."""
    stream = (
        '{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}\n'
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )

    assert turn_from_events(parse_events(stream)).message.content == "done"


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
    assert "hello" in prompt
    assert "# Conversation so far" not in prompt


def test_history_is_labelled_and_the_latest_message_set_apart() -> None:
    """Codex takes one block of text, so the roles a chat API carries
    structurally have to be spelled out or the model cannot tell what it is
    answering from what it is remembering."""
    prompt = render_prompt(
        PROFILE,
        (
            Message.user("what is 2+2"),
            Message.assistant("4"),
            Message.user("and times 3"),
        ),
    )

    assert "# Conversation so far" in prompt
    assert "User: what is 2+2" in prompt
    assert "Assistant: 4" in prompt
    assert prompt.index("# Message to answer") > prompt.index("# Conversation so far")
    assert prompt.endswith("and times 3")


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
