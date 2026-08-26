"""Session configuration: what it normalizes, and what it deliberately omits.

The behaviour worth pinning is that a spec carries intent and nothing else. The
conversation it selects lives in the agent, reachable through an id, which is
what makes a reply arriving on a webhook three days later resume a live
conversation instead of starting a new one.
"""

import pytest

from langgraph_acp import ACPSessionSpec, ACPSessionStrategy


def test_reuse_is_the_default_strategy() -> None:
    assert ACPSessionSpec().strategy is ACPSessionStrategy.REUSE


def test_a_strategy_string_is_the_strategy_it_names() -> None:
    """The documented spelling and the enum are one value, not two."""
    assert ACPSessionSpec(strategy="resume") == ACPSessionSpec(
        strategy=ACPSessionStrategy.RESUME
    )
    assert ACPSessionSpec(strategy="resume").strategy == "resume"


def test_an_unknown_strategy_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="acp-reuse"):
        ACPSessionSpec(strategy="acp-reuse")


def test_a_pinned_session_contradicts_a_new_session() -> None:
    """`new` ignores any binding, so a session id given with it is a mistake."""
    with pytest.raises(ValueError, match="sess_abc123"):
        ACPSessionSpec(strategy="new", session_id="sess_abc123")


def test_a_pinned_session_is_allowed_when_resuming() -> None:
    spec = ACPSessionSpec(strategy="resume", session_id="sess_abc123")

    assert spec.session_id == "sess_abc123"


def test_specs_compare_by_value() -> None:
    assert ACPSessionSpec(key="reviewer") == ACPSessionSpec(key="reviewer")
    assert ACPSessionSpec(key="reviewer") != ACPSessionSpec(key="implementer")


def test_a_spec_holds_no_conversation() -> None:
    """Only identity and intent. History belongs to the agent."""
    assert set(ACPSessionSpec.__dataclass_fields__) == {
        "strategy",
        "key",
        "session_id",
    }
