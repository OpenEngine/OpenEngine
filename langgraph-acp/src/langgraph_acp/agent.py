"""How to reach an ACP agent, and how a name comes to mean one.

A provider is configuration that knows how to produce a connection: which
executable, which environment, which compatibility quirks. It is the only place
in this package where one agent implementation may differ from another, and it
exists so that nothing above it -- the node especially -- ever has to ask which
agent it is talking to.

    ACPAgentRegistry            "codex" -> CodexACPProvider(...)
        | resolve("codex")
        v
    ACPAgentProvider            how to reach that agent
        | connect()
        v
    ACPClient                   one initialized connection

The registry is what keeps `ACPNode(agent="codex")` honest. A string in a graph
definition is worth having only if applications can put their own agents behind
one, so registration is public and the built-in set is small.

Providers carry their own name rather than being registered under one supplied
separately. The name travels: it stamps every event and every error raised
against that agent, so a provider registered as `"codex"` while calling itself
something else would mislabel both.
"""

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from langgraph_acp._json import checked_sequence
from langgraph_acp._stdio import connect_over_stdio
from langgraph_acp.client import ACPClient
from langgraph_acp.errors import ACPAgentNotFoundError


def launch_command(command: Sequence[str]) -> tuple[str, ...]:
    """An executable and its arguments, checked before anything tries to run them.

    Shared by every provider, because every provider stores one and the two
    ways to get it wrong -- an empty list, or a whole command line handed in as
    a single string -- are worth catching where the graph is written rather than
    on the first connection attempt.
    """
    launched = tuple(checked_sequence(command, field="command"))
    if not launched:
        raise ValueError("command must name the executable to launch")
    return launched


@runtime_checkable
class ACPAgentProvider(Protocol):
    """Everything needed to reach one ACP agent implementation.

    Providers are reusable and hold no connection state, so one registered
    instance serves every node in every graph; `connect` is what produces the
    thing with a lifetime.
    """

    @property
    def name(self) -> str:
        """The name this agent answers to, as a graph would write it."""
        ...

    async def connect(self) -> ACPClient:
        """Reach the agent and complete the ACP handshake.

        Returns a client only once the agent has answered `initialize`, so a
        caller never holds a connection whose capabilities are unknown.
        """
        ...


@dataclass(frozen=True, slots=True)
class StdioACPProvider:
    """An agent launched as a child process and spoken to over its stdio.

    The generic provider, and the one an application registering its own ACP
    agent almost always wants:

        registry.register(
            StdioACPProvider(name="gemini", command=["gemini", "--experimental-acp"])
        )

    Every ACP CLI works this way, so a new agent is usually a command line
    rather than a class.
    """

    name: str
    command: Sequence[str]
    """The executable and its arguments, as `subprocess` takes them."""
    env: Mapping[str, str] | None = None
    """Overlaid on this process's environment, never replacing it."""
    cwd: str | os.PathLike[str] | None = None
    """Where to launch the process. Not the workspace a session is given."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))

    async def connect(self) -> ACPClient:
        return await connect_over_stdio(
            agent=self.name, command=self.command, env=self.env, cwd=self.cwd
        )


class ACPAgentRegistry:
    """Names, and the providers they resolve to.

    Mutable on purpose: an application registers its agents during startup, and
    a graph written months earlier still says `agent="codex"`.
    """

    def __init__(self, providers: Iterable[ACPAgentProvider] = ()) -> None:
        self._providers: dict[str, ACPAgentProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ACPAgentProvider, *, replace: bool = False) -> None:
        """Register `provider` under its own name.

        Registering over an existing name needs `replace=True`. Silently
        shadowing an agent would mean a graph resolving to something other than
        what its author read in the registry.
        """
        if provider.name in self._providers and not replace:
            raise ValueError(
                f"{provider.name!r} is already registered; pass replace=True to "
                "mean it, or give this provider a name of its own"
            )
        self._providers[provider.name] = provider

    def resolve(self, name: str) -> ACPAgentProvider:
        """The provider registered as `name`."""
        provider = self._providers.get(name)
        if provider is None:
            known = ", ".join(sorted(self._providers)) or "none"
            raise ACPAgentNotFoundError(
                f"no ACP agent is registered as {name!r} (registered: {known})",
                agent=name,
            )
        return provider

    @property
    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted."""
        return tuple(sorted(self._providers))

    def __contains__(self, name: object) -> bool:
        return name in self._providers


_DEFAULT: ACPAgentRegistry | None = None


def default_registry() -> ACPAgentRegistry:
    """The registry a node uses when it was given no other one.

    Shared and mutable, so registering an application's own agents once makes
    them resolvable everywhere:

        default_registry().register(StdioACPProvider(name="gemini", command=[...]))

    The built-in providers are imported here rather than at module scope: this
    module defines the abstraction they implement, and importing them at the top
    would make the abstraction depend on its instances.
    """
    global _DEFAULT
    if _DEFAULT is None:
        from langgraph_acp.providers.codex import CodexACPProvider

        _DEFAULT = ACPAgentRegistry([CodexACPProvider()])
    return _DEFAULT


__all__ = [
    "ACPAgentProvider",
    "ACPAgentRegistry",
    "StdioACPProvider",
    "default_registry",
]
