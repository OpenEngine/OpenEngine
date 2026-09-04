"""Communications capability.

Getting messages to humans -- status updates, review requests, failure notices.
Buzz is the intended first implementation; Slack, email, or a log line satisfy
the same shape.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from engine.domain.ids import RunId


@dataclass(frozen=True, slots=True)
class MessageLink:
    """A provider-neutral link included in a human-facing message."""

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Message:
    """Structured message content rendered by a communications adapter."""

    text: str
    links: tuple[MessageLink, ...] = ()


@runtime_checkable
class Communications(Protocol):
    """Delivers messages to humans on behalf of a run."""

    async def post(
        self, channel: str, message: str | Message, run_id: RunId | None = None
    ) -> str:
        """Send a message. Returns a provider-specific message id."""
        ...

    async def reply(self, message_id: str, message: str) -> str:
        """Thread a follow-up under an earlier message."""
        ...


__all__ = ["Communications", "Message", "MessageLink"]
