"""Conversations: what was said, and who said it.

The conversation is ours, not the model provider's. An adapter may keep a native
session open for efficiency, but the source of truth is a `Conversation` in the
state store -- otherwise history lives inside a vendor's process and the agent
cannot be resumed, inspected, or moved to another provider.

The vocabulary here is deliberately the lowest common denominator of chat APIs
(roles, content, tool calls) rather than any one provider's schema. Translating
into OpenAI's wire format, or anyone else's, is an adapter's job.
"""

from dataclasses import dataclass, field, replace
from enum import Enum

from engine.domain.ids import AgentInstanceId, ConversationId, MessageId


class Role(Enum):
    """Who produced a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke one tool.

    `arguments` stays as the model produced it, unparsed. Two reasons: the domain
    has no business owning a JSON codec, and malformed arguments have to survive
    intact long enough to be handed back to the model as a tool error.
    """

    call_id: str
    name: str
    arguments: str = "{}"


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a conversation.

    `tool_calls` is only ever populated on an assistant message; `tool_call_id`
    only on a tool message, tying a result back to the request that asked for it.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default=())
    tool_call_id: str | None = None
    message_id: MessageId | None = None

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls, content: str = "", tool_calls: tuple[ToolCall, ...] = ()
    ) -> "Message":
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, call_id: str, content: str) -> "Message":
        """The answer to one `ToolCall`, addressed by its id."""
        return cls(role=Role.TOOL, content=content, tool_call_id=call_id)

    def with_id(self, message_id: MessageId) -> "Message":
        """Same message, stamped by whoever stored it."""
        return replace(self, message_id=message_id)


@dataclass(frozen=True, slots=True)
class Conversation:
    """The full history belonging to one agent instance.

    Immutable, like everything else in the domain: appending returns a new
    conversation rather than mutating this one, so a conversation can be passed
    to a runner without any risk of it being edited underneath the caller.
    """

    conversation_id: ConversationId
    instance_id: AgentInstanceId
    messages: tuple[Message, ...] = field(default=())

    def appending(self, *messages: Message) -> "Conversation":
        return replace(self, messages=(*self.messages, *messages))

    @property
    def last(self) -> Message | None:
        return self.messages[-1] if self.messages else None


__all__ = ["Conversation", "Message", "Role", "ToolCall"]
