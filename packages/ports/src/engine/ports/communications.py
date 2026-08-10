"""Communications capability.

Getting messages to humans -- status updates, review requests, failure notices.
Buzz is the intended first implementation; Slack, email, or a log line satisfy
the same shape.
"""

from typing import Protocol, runtime_checkable

from engine.domain.ids import RunId


@runtime_checkable
class Communications(Protocol):
    """Delivers messages to humans on behalf of a run."""

    async def post(self, channel: str, message: str, run_id: RunId | None = None) -> str:
        """Send a message. Returns a provider-specific message id."""
        ...

    async def reply(self, message_id: str, message: str) -> str:
        """Thread a follow-up under an earlier message."""
        ...


__all__ = ["Communications"]
