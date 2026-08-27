"""The minimal LangGraph node that runs one ACP turn.

`ACPNode` is deliberately an async callable rather than a LangGraph subclass.
LangGraph accepts async callables as nodes, and keeping that boundary structural
means importing this package does not import LangGraph or add it as a runtime
dependency.

This first version starts a fresh connection and session for every invocation.
Session reuse, runtime prompt resolvers, event streaming, and the rest of the
configuration surface arrive in their own tickets; the useful end-to-end path
is already complete here:

    resolve -> connect and initialize -> session/new -> session/prompt -> result
"""

from collections.abc import Mapping
from dataclasses import dataclass

from langgraph_acp._json import JSONValue, copied_mapping
from langgraph_acp.agent import ACPAgentRegistry, default_registry
from langgraph_acp.events import ACPEventType
from langgraph_acp.result import ACPResult
from langgraph_acp.session import ACPPrompt


@dataclass(frozen=True, slots=True, kw_only=True)
class ACPNode:
    """Run an ACP agent as an async LangGraph-compatible node.

    The value passed to the node is the prompt for this minimal milestone:

        result = await ACPNode(agent="codex")("Review this change")

    A registry may be supplied by applications that register their own agents,
    and lets tests point the complete process boundary at a stub ACP CLI.
    """

    agent: str
    """The provider name to resolve when the node is invoked."""
    registry: ACPAgentRegistry | None = None
    """The registry to resolve against; the shared default when omitted."""

    async def __call__(self, prompt: ACPPrompt) -> ACPResult:
        provider = (self.registry or default_registry()).resolve(self.agent)
        client = await provider.connect()
        try:
            session = await client.new_session()
            message_parts: list[str] = []
            content: list[JSONValue] = []
            stop_reason: str | None = None

            async for event in session.prompt(prompt):
                if event.type == ACPEventType.MESSAGE_DELTA:
                    block = event.data.get("content")
                    if isinstance(block, Mapping):
                        copied = copied_mapping(block)
                        content.append(copied)
                        text = copied.get("text")
                        if copied.get("type") == "text" and isinstance(text, str):
                            message_parts.append(text)
                elif event.type == ACPEventType.PROMPT_COMPLETED:
                    reported = event.data.get("stopReason")
                    if isinstance(reported, str):
                        stop_reason = reported

            return ACPResult(
                message="".join(message_parts),
                content=content,
                agent=client.agent,
                session_id=session.session_id,
                stop_reason=stop_reason,
            )
        finally:
            await client.close()


__all__ = ["ACPNode"]
