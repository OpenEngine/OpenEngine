"""What a turn returns, and what its numbers mean.

Two behaviours matter enough to pin: a result survives the trip through
LangGraph state that a checkpoint puts it through, and unreported usage stays
unreported rather than becoming zero.
"""

import json
from dataclasses import fields

import pytest

from langgraph_acp import ACPResult, ACPSessionRef, ACPUsage


def test_a_result_defaults_to_saying_nothing_happened_yet() -> None:
    result = ACPResult()

    assert result.message == ""
    assert result.content == ()
    assert result.session is None
    assert result.stop_reason is None
    assert result.tool_calls == ()
    assert result.metadata == {}


def test_usage_is_always_present_even_when_empty() -> None:
    """So no consumer has to guard a `None` before reading a token count."""
    assert ACPResult().usage == ACPUsage()


def test_unreported_usage_is_absent_not_zero() -> None:
    """An aggregate built over invented zeros is wrong and looks fine."""
    usage = ACPUsage(input_tokens=120)

    assert usage.output_tokens is None
    assert usage.to_dict() == {"input_tokens": 120}


def test_usage_survives_a_round_trip() -> None:
    usage = ACPUsage(
        input_tokens=1200,
        output_tokens=340,
        thought_tokens=80,
        cached_tokens=900,
        context_used=4500,
        context_size=200_000,
        cost_usd=0.0123,
    )

    assert ACPUsage.from_dict(usage.to_dict()) == usage


def test_every_usage_field_survives_the_round_trip() -> None:
    """Driven off the dataclass, so a field added to only one side is caught."""
    reported = {f.name: 3 for f in fields(ACPUsage)}

    assert ACPUsage.from_dict(reported).to_dict() == reported


def test_usage_that_is_not_a_number_is_refused_where_it_is_read() -> None:
    """A store handing back `"1200"` should fail here, not in whatever sums it."""
    with pytest.raises(TypeError, match="input_tokens"):
        ACPUsage.from_dict({"input_tokens": "1200"})


def test_a_zero_reported_by_the_agent_is_kept() -> None:
    assert ACPUsage.from_dict({"output_tokens": 0}).output_tokens == 0
    assert ACPUsage(output_tokens=0).to_dict() == {"output_tokens": 0}


def test_a_result_survives_a_round_trip_through_state() -> None:
    result = ACPResult(
        message="Reviewed the change.",
        content=({"type": "text", "text": "Reviewed the change."},),
        session=ACPSessionRef(agent="codex", session_id="sess_abc123", key="reviewer"),
        stop_reason="end_turn",
        usage=ACPUsage(input_tokens=1200, output_tokens=340),
        tool_calls=({"id": "call_1", "name": "read_file"},),
        metadata={"attempt": 2},
    )

    assert ACPResult.from_dict(result.to_dict()) == result


def test_a_result_serializes_to_json() -> None:
    """LangGraph checkpoints what a node returns, so it has to be JSON-shaped."""
    result = ACPResult(
        message="Done.",
        session=ACPSessionRef(agent="claude", session_id="sess_1", key="reviewer"),
        usage=ACPUsage(output_tokens=7),
    )

    assert json.loads(json.dumps(result.to_dict()))["session"]["session_id"] == "sess_1"


def test_a_result_does_not_share_the_containers_it_was_given() -> None:
    """A caller's later mutation must not rewrite a result already returned."""
    metadata = {"attempt": 1}
    content = [{"type": "text", "text": "hello"}]
    result = ACPResult(message="hello", content=content, metadata=metadata)

    metadata["attempt"] = 2
    content.append({"type": "text", "text": "goodbye"})

    assert result.metadata == {"attempt": 1}
    assert result.content == ({"type": "text", "text": "hello"},)


def test_a_result_does_not_share_a_nested_container_either() -> None:
    """Content blocks are nested, so a one-level copy would isolate nothing."""
    block = {"type": "text", "text": "hello"}
    result = ACPResult(message="hello", content=[block])

    block["text"] = "MUTATED"

    assert result.content == ({"type": "text", "text": "hello"},)


def test_the_state_view_shares_nothing_back() -> None:
    """A caller that normalizes the dict it was handed is not editing the result."""
    result = ACPResult(message="hello", content=[{"type": "text", "text": "hello"}])

    content = result.to_dict()["content"]
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, dict)
    block["text"] = "MUTATED"

    assert result.content == ({"type": "text", "text": "hello"},)


def test_a_lone_string_is_refused_where_content_blocks_belong() -> None:
    """A `str` is a sequence, so without the guard it becomes one block per character."""
    with pytest.raises(TypeError, match="content"):
        ACPResult(content="Reviewed the change.")


def test_a_lone_string_is_refused_where_tool_calls_belong() -> None:
    with pytest.raises(TypeError, match="tool_calls"):
        ACPResult(tool_calls="read_file")


def test_a_lone_string_read_back_from_state_is_refused_too() -> None:
    with pytest.raises(TypeError, match="content"):
        ACPResult.from_dict({"content": "Reviewed the change."})


def test_results_compare_by_value() -> None:
    assert ACPResult(message="ok") == ACPResult(message="ok")
    assert ACPResult(message="ok") != ACPResult(message="ok", stop_reason="cancelled")
