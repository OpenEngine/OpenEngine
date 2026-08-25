"""One live connection to an ACP agent, and what that agent said it can do.

The shape is three objects deep, and each one owns a different lifetime:

    ACPAgentProvider   how to reach an agent          (configuration)
        | connect()
        v
    ACPClient          one initialized connection     (a process, a pipe)
        | new_session() / resume_session()
        v
    ACPSession         one conversation               (a session id, turns)

A client is a connection, not a conversation. Several sessions can share one --
that is what makes running an implementer and three reviewers against a single
agent process reasonable -- and closing the client ends all of them, while
closing a session ends none of the others.

Capabilities are read once, during initialization, and kept. They are the
protocol's forward-compatibility mechanism: an agent that omits a capability is
saying it does not have it, so every flag here defaults to `False` and an
unfamiliar one survives in `raw` rather than being dropped.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from langgraph_acp._json import JSONObject, JSONValue, copied_mapping
from langgraph_acp.session import ACPSession

#: The ACP revision this client speaks. Versions mark breaking changes only;
#: everything else is negotiated through capabilities.
PROTOCOL_VERSION = 1


def _flag(capabilities: Mapping[str, JSONValue], name: str) -> bool:
    return capabilities.get(name) is True


def _nested(capabilities: Mapping[str, JSONValue], name: str) -> JSONObject:
    nested = capabilities.get(name)
    return dict(nested) if isinstance(nested, Mapping) else {}


@dataclass(frozen=True, slots=True)
class ACPCapabilities:
    """What an agent advertised when the connection was initialized.

    Every field defaults to "not supported", because that is what the protocol
    says an omission means. Reading a missing capability as absent rather than
    as unknown is what lets a workflow fail early and by name instead of
    discovering the gap halfway through a turn.

    `raw` keeps the whole initialize result. A capability this version has no
    field for is still visible there, so an agent that grows one does not have
    to wait for a release here to be usable.
    """

    protocol_version: int = 0
    """The ACP revision the agent agreed to speak."""
    load_session: bool = False
    """The agent can continue an earlier session -- ACP's `loadSession`."""
    prompt_image: bool = False
    """Prompts may carry image content blocks."""
    prompt_audio: bool = False
    """Prompts may carry audio content blocks."""
    prompt_embedded_context: bool = False
    """Prompts may carry embedded resource content blocks."""
    mcp_http: bool = False
    """MCP servers may be supplied over HTTP. Stdio needs no capability."""
    mcp_sse: bool = False
    """MCP servers may be supplied over SSE."""
    auth_methods: Sequence[str] = ()
    """Identifiers of the authentication methods the agent offers."""
    raw: Mapping[str, JSONValue] = field(default_factory=dict)
    """The initialize result as the agent sent it."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "auth_methods", tuple(self.auth_methods))
        object.__setattr__(self, "raw", copied_mapping(self.raw))

    @classmethod
    def from_initialize_response(
        cls, response: Mapping[str, JSONValue]
    ) -> "ACPCapabilities":
        """Read an ACP `initialize` result, tolerating everything optional."""
        agent = _nested(response, "agentCapabilities")
        prompt = _nested(agent, "promptCapabilities")
        mcp = _nested(agent, "mcpCapabilities")
        version = response.get("protocolVersion")
        methods = response.get("authMethods")
        return cls(
            protocol_version=version if isinstance(version, int) else 0,
            load_session=_flag(agent, "loadSession"),
            prompt_image=_flag(prompt, "image"),
            prompt_audio=_flag(prompt, "audio"),
            prompt_embedded_context=_flag(prompt, "embeddedContext"),
            mcp_http=_flag(mcp, "http"),
            mcp_sse=_flag(mcp, "sse"),
            auth_methods=tuple(
                str(method["id"])
                for method in (methods if isinstance(methods, Sequence) else ())
                if isinstance(method, Mapping) and "id" in method
            ),
            raw=response,
        )


@runtime_checkable
class ACPClient(Protocol):
    """An initialized connection to one ACP agent.

    A client exists only after `initialize` has been answered, so
    `capabilities` is always the agent's own answer and never a guess.
    """

    @property
    def agent(self) -> str:
        """The registered name of the agent on the other end."""
        ...

    @property
    def capabilities(self) -> ACPCapabilities:
        """What the agent advertised during initialization."""
        ...

    async def new_session(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: Sequence[Mapping[str, JSONValue]] = (),
    ) -> ACPSession:
        """Start a conversation. `cwd` defaults to the current directory.

        `mcp_servers` entries are ACP `mcpServers` objects, passed through as
        given; the `MCPServer` type that builds them arrives with its own
        ticket, and inventing half of it here would only have to be undone.
        """
        ...

    async def resume_session(
        self,
        session_id: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: Sequence[Mapping[str, JSONValue]] = (),
    ) -> ACPSession:
        """Continue a conversation the agent already has.

        Raises `ACPAgentCapabilityError` when the agent does not advertise
        `loadSession`, before anything is sent.
        """
        ...

    async def close(self) -> None:
        """End the connection and every session on it. Safe to call twice."""
        ...


__all__ = ["PROTOCOL_VERSION", "ACPCapabilities", "ACPClient"]
