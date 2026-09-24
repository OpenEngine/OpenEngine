"""The other request an agent makes that only a person can settle: a question.

`session/request_permission` asks whether the agent may do something;
`elicitation/create` asks what it should do -- Claude's `AskUserQuestion`, a
Codex `request_user_input`, an MCP server's form. The agent sends a JSON schema
describing the answer it wants and waits for content matching it.

Like permissions, the answer is a handler supplied by whoever built the
provider. Unlike permissions there is no safe default answer, so without a
handler the capability is simply not advertised: an agent told the client cannot
render a form keeps the question to itself (Claude disables `AskUserQuestion`,
Codex answers its own `request_user_input` with nothing) rather than hanging.

Only form elicitations are advertised. A URL elicitation sends a person to a web
page, and this client has nowhere to send them.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from langgraph_acp._json import JSONObject, JSONValue, as_mapping, copied_mapping

#: The `clientCapabilities.elicitation` a client with a handler advertises.
FORM_ELICITATION_CAPABILITY: JSONObject = {"form": {}}


@dataclass(frozen=True, slots=True)
class ACPElicitationRequest:
    """An agent, mid-turn, waiting for structured input."""

    agent: str
    session_id: str | None = None
    tool_call_id: str | None = None
    """The tool call the question belongs to, when the agent says."""
    mode: str = "form"
    message: str = ""
    requested_schema: Mapping[str, JSONValue] = field(default_factory=dict)
    """A restricted JSON schema: an object of flat, primitive properties."""
    params: Mapping[str, JSONValue] = field(default_factory=dict)
    """The whole request, for anything these fields do not name."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_schema", copied_mapping(self.requested_schema))
        object.__setattr__(self, "params", copied_mapping(self.params))

    @classmethod
    def from_params(
        cls, agent: str, params: Mapping[str, JSONValue]
    ) -> "ACPElicitationRequest":
        """Read an ACP `elicitation/create` payload, tolerating what it can."""
        copied = copied_mapping(params)
        session_id = copied.get("sessionId")
        tool_call_id = copied.get("toolCallId")
        mode = copied.get("mode")
        message = copied.get("message")
        schema = copied.get("requestedSchema")
        return cls(
            agent=agent,
            session_id=session_id if isinstance(session_id, str) else None,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            mode=mode if isinstance(mode, str) else "form",
            message=message if isinstance(message, str) else "",
            requested_schema=(
                as_mapping(schema, field="requestedSchema")
                if isinstance(schema, Mapping)
                else {}
            ),
            params=copied,
        )


@dataclass(frozen=True, slots=True)
class ACPElicitationResponse:
    """What the client answered: content, a refusal to answer, or cancellation.

    `decline` and `cancel` differ the way they do in MCP: declining is a person
    saying no, and the agent carries on without the answer; cancelling abandons
    the thing that asked.
    """

    action: str
    content: Mapping[str, JSONValue] | None = None

    @classmethod
    def accept(cls, content: Mapping[str, JSONValue]) -> "ACPElicitationResponse":
        return cls(action="accept", content=copied_mapping(content))

    @classmethod
    def decline(cls) -> "ACPElicitationResponse":
        return cls(action="decline")

    @classmethod
    def cancel(cls) -> "ACPElicitationResponse":
        return cls(action="cancel")

    def to_acp(self) -> JSONObject:
        """The `elicitation/create` result the agent is waiting for."""
        if self.action == "accept":
            return {"action": "accept", "content": dict(self.content or {})}
        return {"action": self.action}


ACPElicitationHandler: TypeAlias = Callable[
    [ACPElicitationRequest], Awaitable[ACPElicitationResponse]
]
"""How a connection answers `elicitation/create`."""


__all__ = [
    "ACPElicitationHandler",
    "ACPElicitationRequest",
    "ACPElicitationResponse",
]
