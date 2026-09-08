"""The one request an agent makes that only a person can settle.

An ACP agent asks `session/request_permission` before it does something it
believes a human should authorize -- running a command, writing a file, calling
a tool the client did not pre-approve. Everything else in a turn is the agent
telling the client what happened; this is the agent waiting on an answer, and
the connection cannot make one up.

So the answer is a *handler*, supplied by whoever built the provider, and the
default is `deny_permission`. Declining is the only answer that cannot approve
something nobody was asked about, and an unanswered request would hang the turn
instead of failing it.

    async def ask_the_user(request: ACPPermissionRequest) -> ACPPermissionOutcome:
        chosen = await somewhere.wait_for(request)
        return ACPPermissionOutcome.selected(chosen)

    StdioACPProvider(name="codex", command=[...], permissions=ask_the_user)

The handler may take as long as it likes, including never returning. That is
deliberate: a caller that wants to stop the process while a person thinks it
over writes down what it needs, awaits something that will not be set, and lets
its cancellation tear the connection down. See `langgraph_acp.continuation` for
what "writes down what it needs" means and how the conversation is picked back
up afterwards.

The request is normalized only as far as it can be. `options` is the list the
agent offered and an answer has to name one of them, so it is read out into
fields; the rest of the payload is agent-shaped and is kept whole in `params`,
because a handler that wants to render the diff an agent attached should not
have to wait for a release here.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    as_mapping,
    as_sequence,
    copied_mapping,
)

#: What ACP calls a cancelled permission request: the client declined to answer,
#: as distinct from having chosen one of the options the agent offered.
CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ACPPermissionOption:
    """One answer the agent said it would accept.

    `option_id` is the only part the protocol cares about; `name` and `kind` are
    what a client shows a person, and both are optional because an agent that
    omits them is still asking a real question.
    """

    option_id: str
    name: str = ""
    kind: str = ""
    """The agent's classification: `allow_once`, `allow_always`, `reject_once`."""


@dataclass(frozen=True, slots=True)
class ACPPermissionRequest:
    """An agent, mid-turn, waiting to be told whether it may proceed."""

    agent: str
    session_id: str | None = None
    options: tuple[ACPPermissionOption, ...] = ()
    """The answers the agent offered, in the order it offered them."""
    tool_call: Mapping[str, JSONValue] = field(default_factory=dict)
    """What it wants to do, as the agent described it."""
    params: Mapping[str, JSONValue] = field(default_factory=dict)
    """The whole request, for anything these fields do not name."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "tool_call", copied_mapping(self.tool_call))
        object.__setattr__(self, "params", copied_mapping(self.params))

    @classmethod
    def from_params(
        cls, agent: str, params: Mapping[str, JSONValue]
    ) -> "ACPPermissionRequest":
        """Read an ACP `session/request_permission` payload.

        Tolerant on purpose: an agent that offers a malformed option is still
        asking, and dropping the option is better than dropping the question.
        """
        copied = copied_mapping(params)
        session_id = copied.get("sessionId")
        return cls(
            agent=agent,
            session_id=session_id if isinstance(session_id, str) else None,
            options=tuple(
                option
                for option in (
                    _option(entry)
                    for entry in as_sequence(copied.get("options"), field="options")
                )
                if option is not None
            ),
            tool_call=as_mapping(copied.get("toolCall"), field="toolCall"),
            params=copied,
        )


def _option(entry: JSONValue) -> ACPPermissionOption | None:
    if not isinstance(entry, Mapping):
        return None
    option_id = entry.get("optionId")
    if not isinstance(option_id, str) or not option_id:
        return None
    name = entry.get("name")
    kind = entry.get("kind")
    return ACPPermissionOption(
        option_id=option_id,
        name=name if isinstance(name, str) else "",
        kind=kind if isinstance(kind, str) else "",
    )


@dataclass(frozen=True, slots=True)
class ACPPermissionOutcome:
    """What the client decided, in the shape ACP wants it back.

    Two states rather than a boolean: choosing an option is naming one of the
    agent's own answers, and declining to answer at all is a third thing that no
    `optionId` spells.
    """

    option_id: str | None = None
    """The option chosen, or `None` for a request that was refused outright."""

    @classmethod
    def selected(cls, option_id: str) -> "ACPPermissionOutcome":
        return cls(option_id=option_id)

    @classmethod
    def cancelled(cls) -> "ACPPermissionOutcome":
        return cls(option_id=None)

    @property
    def granted(self) -> bool:
        return self.option_id is not None

    def to_acp(self) -> JSONObject:
        """The `session/request_permission` result the agent is waiting for."""
        if self.option_id is None:
            return {"outcome": {"outcome": CANCELLED}}
        return {"outcome": {"outcome": "selected", "optionId": self.option_id}}


ACPPermissionHandler: TypeAlias = Callable[
    [ACPPermissionRequest], Awaitable[ACPPermissionOutcome]
]
"""How a connection answers `session/request_permission`."""


async def deny_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Refuse. The default, and the only safe answer nobody configured."""
    return ACPPermissionOutcome.cancelled()


__all__ = [
    "CANCELLED",
    "ACPPermissionHandler",
    "ACPPermissionOption",
    "ACPPermissionOutcome",
    "ACPPermissionRequest",
    "deny_permission",
]
