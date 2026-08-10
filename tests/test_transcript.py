"""Flattening a conversation for a CLI-shaped agent.

Shared by every runner that takes a prompt rather than a message array, so the
append-only invariant is tested once here rather than once per adapter -- which
is the reason the code is shared at all.
"""

import pytest

from engine.domain import Message, ToolCall
from engine.runtime.transcript import MAX_REPLAYED_OUTPUT_CHARS, flatten, render_message


def test_roles_are_spelled_out() -> None:
    text = flatten((Message.user("hello"), Message.assistant("hi")))

    assert "User: hello" in text
    assert "Assistant: hi" in text


def test_each_turn_extends_the_last_rather_than_rewriting_it() -> None:
    """The precondition for a prompt-cache hit, and the reason nothing may be
    reordered or reworded once written."""
    first = (Message.user("one"),)
    second = (*first, Message.assistant("two"), Message.user("three"))
    third = (*second, Message.assistant("four"), Message.user("five"))

    texts = [flatten(messages) for messages in (first, second, third)]

    assert texts[1].startswith(texts[0])
    assert texts[2].startswith(texts[1])


def test_a_tool_call_renders_as_what_was_run() -> None:
    call = ToolCall(call_id="c1", name="Glob", arguments='{"pattern": "*.py"}')

    text = render_message(Message.assistant(tool_calls=(call,)))

    assert text == 'Assistant ran Glob: {"pattern": "*.py"}'


def test_a_long_output_is_truncated_on_replay_but_never_edited() -> None:
    """Storage stays complete; only what a later turn re-reads is bounded."""
    dump = Message.tool_result("c1", "x" * 5000)

    text = render_message(dump)

    assert len(text) < MAX_REPLAYED_OUTPUT_CHARS + 200
    assert "4000 more characters, stored in full" in text
    assert dump.content == "x" * 5000


def test_short_output_is_left_alone() -> None:
    assert render_message(Message.tool_result("c1", "brief")) == "Tool result: brief"


def test_empty_messages_are_dropped() -> None:
    """A bare role label reads to a model as an empty turn."""
    text = flatten((Message.user("hello"), Message.assistant(""), Message.user("still there?")))

    assert "Assistant:" not in text


def test_an_empty_conversation_is_refused() -> None:
    with pytest.raises(ValueError):
        flatten(())
