"""Streamed events: naming, ordering, and forward compatibility.

The namespaced name is the contract a consumer subscribes to, and `acp.raw` is
the promise that an ACP addition this library has never heard of still arrives
rather than raising.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from langgraph_acp import EVENT_NAMESPACE, ACPEvent, ACPEventType

STAMP = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_an_event_carries_the_namespaced_name() -> None:
    event = ACPEvent(agent="codex", type=ACPEventType.TOOL_UPDATED, timestamp=STAMP)

    assert event.type == "tool.updated"
    assert event.name == "acp.tool.updated"


def test_both_spellings_of_a_name_are_the_same_event() -> None:
    """A producer writing the prefix and one omitting it agree."""
    written = ACPEvent(agent="codex", type="acp.message.delta", timestamp=STAMP)
    plain = ACPEvent(agent="codex", type="message.delta", timestamp=STAMP)

    assert written == plain
    assert written.type == "message.delta"


def test_an_unknown_type_is_carried_rather_than_rejected() -> None:
    """ACP will grow updates this version has never seen; they still stream."""
    event = ACPEvent(
        agent="codex",
        type=ACPEventType.RAW,
        timestamp=STAMP,
        data={"update": {"sessionUpdate": "something_new"}},
    )

    assert event.name == f"{EVENT_NAMESPACE}.raw"
    assert event.data["update"] == {"sessionUpdate": "something_new"}


def test_an_event_type_is_the_string_it_names() -> None:
    """A `StrEnum`, so a member goes wherever its spelling would."""
    name: str = ACPEventType.PERMISSION_REQUESTED

    assert name == "permission.requested"


def test_the_documented_vocabulary_is_present() -> None:
    """The plan's event list, so a missing one is a failure and not a surprise."""
    assert {t.value for t in ACPEventType} == {
        "session.started",
        "session.resumed",
        "session.closed",
        "session.info_updated",
        "message.delta",
        "message.completed",
        "thought.delta",
        "tool.started",
        "tool.updated",
        "tool.completed",
        "plan.updated",
        "permission.requested",
        "permission.resolved",
        "elicitation.requested",
        "elicitation.resolved",
        "config.updated",
        "usage.updated",
        "prompt.completed",
        "error",
        "raw",
    }


def test_an_event_survives_a_round_trip() -> None:
    event = ACPEvent(
        agent="codex",
        type=ACPEventType.TOOL_STARTED,
        session_id="sess_abc123",
        thread_id="pr-918",
        node="review",
        timestamp=STAMP,
        data={"tool": "read_file"},
    )

    assert ACPEvent.from_dict(event.to_dict()) == event


def test_the_wire_form_names_the_event_the_way_a_consumer_selects_it() -> None:
    event = ACPEvent(agent="codex", type="prompt.completed", timestamp=STAMP)

    assert event.to_dict()["type"] == "acp.prompt.completed"
    assert event.to_dict()["timestamp"] == "2026-08-25T12:00:00+00:00"


def test_a_naive_timestamp_is_refused() -> None:
    """Events from several workers are ordered against each other."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ACPEvent(agent="codex", type="error", timestamp=datetime(2026, 8, 25, 12, 0))


def test_a_non_utc_timestamp_is_kept_and_stays_comparable() -> None:
    """Any offset will do; only an absent one makes an event unorderable."""
    elsewhere = STAMP.astimezone(timezone(timedelta(hours=-7)))
    event = ACPEvent(agent="codex", type="error", timestamp=elsewhere)

    assert event.timestamp.utcoffset() == timedelta(hours=-7)
    assert event.timestamp == STAMP


def test_an_event_is_stamped_when_the_producer_does_not_stamp_it() -> None:
    before = datetime.now(UTC)
    event = ACPEvent(agent="codex", type=ACPEventType.MESSAGE_DELTA)

    assert before <= event.timestamp <= datetime.now(UTC)


def test_an_event_does_not_share_the_payload_it_was_given() -> None:
    """A streamed event is a fact; the producer's next mutation is not part of it."""
    payload = {"text": "hel"}
    event = ACPEvent(agent="codex", type="message.delta", data=payload, timestamp=STAMP)

    payload["text"] = "hello"

    assert event.data == {"text": "hel"}


def test_an_event_does_not_share_a_nested_payload_either() -> None:
    """`{"update": {...}}` is the shape an ACP session update actually has.

    A one-level copy would isolate the wrapper and leave everything worth
    reading shared.
    """
    update = {"sessionUpdate": "tool_call", "status": "pending"}
    event = ACPEvent(agent="codex", type="raw", data={"update": update}, timestamp=STAMP)

    update["status"] = "completed"

    assert event.data == {"update": {"sessionUpdate": "tool_call", "status": "pending"}}


def test_the_wire_form_shares_nothing_back() -> None:
    """A consumer that redacts what it was handed is not editing the event."""
    event = ACPEvent(
        agent="codex",
        type="raw",
        data={"update": {"sessionUpdate": "tool_call"}},
        timestamp=STAMP,
    )

    payload = event.to_dict()["data"]
    assert isinstance(payload, dict)
    update = payload["update"]
    assert isinstance(update, dict)
    update["sessionUpdate"] = "REDACTED"

    assert event.data == {"update": {"sessionUpdate": "tool_call"}}
