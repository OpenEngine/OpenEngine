"""Typed failures, each carrying the context needed to place it.

An ACP failure is rarely informative on its own. "session/prompt failed" is a
sentence about nothing until it says which agent, which LangGraph thread, and
which logical conversation -- the same failure means "that agent is
misconfigured" or "that one review is stuck" depending on answers the message
does not contain. So every error here accepts the same optional context and
renders whatever was supplied.

Nothing in this module resolves or formats secret material, and callers must not
interpolate credentials into a message: this text is written for ordinary logs.
"""

#: The context an ACP failure may carry, in the order it renders.
_CONTEXT_FIELDS = ("agent", "node", "thread_id", "session_key", "session_id", "operation")


class ACPError(Exception):
    """Base class for every failure this package raises.

    Catching `ACPError` catches anything the adapter itself reports. It does not
    catch a `ValueError` from a malformed constructor argument, which is a bug in
    the graph definition rather than a failure of an ACP conversation.
    """

    def __init__(
        self,
        message: str,
        *,
        agent: str | None = None,
        node: str | None = None,
        thread_id: str | None = None,
        session_key: str | None = None,
        session_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.agent = agent
        """Registered name of the ACP agent involved, when one was resolved."""
        self.node = node
        """LangGraph node that was executing."""
        self.thread_id = thread_id
        """LangGraph thread the invocation belonged to."""
        self.session_key = session_key
        """Logical conversation within that thread."""
        self.session_id = session_id
        """Opaque ACP session identifier, when one existed."""
        self.operation = operation
        """ACP method or adapter step that failed, such as `session/prompt`."""

    @property
    def context(self) -> dict[str, str]:
        """The context that was supplied, omitting what the caller left out."""
        return {
            name: value
            for name in _CONTEXT_FIELDS
            if (value := getattr(self, name)) is not None
        }

    def __str__(self) -> str:
        context = self.context
        if not context:
            return self.message
        rendered = ", ".join(f"{name}={value!r}" for name, value in context.items())
        return f"{self.message} ({rendered})"


class ACPAgentCapabilityError(ACPError):
    """The agent cannot do something the workflow requires.

    Raised while capabilities are being negotiated, before a prompt runs, so a
    workflow that needs resume or MCP against an agent offering neither fails
    where the incompatibility is legible instead of halfway through a turn.
    """


class ACPSessionError(ACPError):
    """A logical conversation could not be created, resumed, or located."""


__all__ = ["ACPAgentCapabilityError", "ACPError", "ACPSessionError"]
