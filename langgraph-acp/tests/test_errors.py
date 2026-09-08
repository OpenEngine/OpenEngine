"""Failures, and the context that makes them readable in a log."""

import pytest

from langgraph_acp import ACPAgentCapabilityError, ACPError, ACPSessionError


def test_every_failure_is_an_acp_error() -> None:
    """One `except` catches anything the adapter reports."""
    assert issubclass(ACPAgentCapabilityError, ACPError)
    assert issubclass(ACPSessionError, ACPError)


def test_a_bare_error_reads_as_its_message() -> None:
    assert str(ACPError("session/prompt failed")) == "session/prompt failed"


def test_an_error_renders_the_context_it_was_given() -> None:
    error = ACPSessionError(
        "no session to resume",
        agent="codex",
        node="review",
        thread_id="pr-918",
        session_key="primary-reviewer",
        operation="session/resume",
    )

    assert str(error) == (
        "no session to resume (agent='codex', node='review', thread_id='pr-918', "
        "session_key='primary-reviewer', operation='session/resume')"
    )


def test_omitted_context_is_not_rendered_as_none() -> None:
    error = ACPError("connection closed", agent="claude")

    assert str(error) == "connection closed (agent='claude')"
    assert error.context == {"agent": "claude"}
    assert error.thread_id is None


def test_context_is_readable_field_by_field() -> None:
    """A handler routing on the failure should not have to parse the message."""
    error = ACPAgentCapabilityError(
        'agent "codex" does not support the required MCP transport',
        agent="codex",
        operation="initialize",
    )

    assert error.agent == "codex"
    assert error.operation == "initialize"
    assert error.message.startswith("agent")


def test_an_acp_error_is_raisable_and_catchable_as_one() -> None:
    with pytest.raises(ACPError) as raised:
        raise ACPSessionError("gone", session_id="sess_abc123")

    assert raised.value.session_id == "sess_abc123"
