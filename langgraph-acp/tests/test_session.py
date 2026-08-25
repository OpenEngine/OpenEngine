"""Session identity: normalization, the store round trip, and what they hold.

The behaviour worth pinning here is that a binding survives a trip through a
store unchanged, because that round trip is what makes a reply on a webhook
three days later resume a live conversation instead of starting a new one.
"""

import pytest

from langgraph_acp import (
    ACPSession,
    ACPSessionBinding,
    ACPSessionRef,
    ACPSessionStrategy,
)


def test_reuse_is_the_default_strategy() -> None:
    assert ACPSession().strategy is ACPSessionStrategy.REUSE


def test_a_strategy_string_is_the_strategy_it_names() -> None:
    """The documented spelling and the enum are one value, not two."""
    assert ACPSession(strategy="resume") == ACPSession(strategy=ACPSessionStrategy.RESUME)
    assert ACPSession(strategy="resume").strategy == "resume"


def test_an_unknown_strategy_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="acp-reuse"):
        ACPSession(strategy="acp-reuse")


def test_a_pinned_session_contradicts_a_new_session() -> None:
    """`new` ignores any binding, so a session id given with it is a mistake."""
    with pytest.raises(ValueError, match="sess_abc123"):
        ACPSession(strategy="new", session_id="sess_abc123")


def test_a_pinned_session_is_allowed_when_resuming() -> None:
    assert ACPSession(strategy="resume", session_id="sess_abc123").session_id == "sess_abc123"


def test_sessions_compare_by_value() -> None:
    assert ACPSession(key="reviewer") == ACPSession(key="reviewer")
    assert ACPSession(key="reviewer") != ACPSession(key="implementer")


def test_a_session_holds_no_conversation() -> None:
    """Only identity and intent. History belongs to the agent."""
    assert {f for f in ACPSession.__dataclass_fields__} == {
        "strategy",
        "key",
        "session_id",
    }


def test_a_binding_survives_a_store_round_trip() -> None:
    binding = ACPSessionBinding(
        thread_id="pr-918",
        session_key="primary-reviewer",
        agent="codex",
        acp_session_id="sess_abc123",
    )

    assert ACPSessionBinding.from_dict(binding.to_dict()) == binding


def test_a_binding_serializes_to_json_scalars() -> None:
    """A store that only takes strings must be able to write every field."""
    binding = ACPSessionBinding(
        thread_id="pr-918",
        session_key="primary-reviewer",
        agent="codex",
        acp_session_id="sess_abc123",
    )

    assert binding.to_dict() == {
        "thread_id": "pr-918",
        "session_key": "primary-reviewer",
        "agent": "codex",
        "acp_session_id": "sess_abc123",
    }


def test_a_binding_names_the_same_session_a_result_does() -> None:
    binding = ACPSessionBinding(
        thread_id="pr-918",
        session_key="primary-reviewer",
        agent="codex",
        acp_session_id="sess_abc123",
    )

    assert binding.ref == ACPSessionRef(
        agent="codex", session_id="sess_abc123", key="primary-reviewer"
    )


def test_bindings_under_one_thread_stay_distinct() -> None:
    """A thread runs several agents; none of them may answer for another."""
    reviewer = ACPSessionBinding("pr-918", "reviewer", "codex", "sess_a")
    implementer = ACPSessionBinding("pr-918", "implementer", "codex", "sess_b")

    assert reviewer != implementer
    assert len({reviewer, implementer}) == 2


def test_a_reference_survives_a_round_trip() -> None:
    ref = ACPSessionRef(agent="claude", session_id="sess_xyz", key="security")

    assert ACPSessionRef.from_dict(ref.to_dict()) == ref
