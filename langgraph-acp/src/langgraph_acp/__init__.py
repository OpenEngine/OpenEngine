"""ACP-compatible agents as first-class LangGraph nodes.

The boundary this package exists to hold:

    LangGraph owns orchestration and workflow durability.
    ACP owns the agent conversation and agent-side session state.
    langgraph-acp owns the binding between the two.

What is here so far is the vocabulary: the types a node configures itself with,
the events it streams, the result it returns, and the failures it reports.
Nothing in this layer connects to an agent, and nothing names one -- `"codex"`
and `"claude"` are strings a registry resolves in a later ticket, never imports
made here.

Serialization follows one rule, so "does this have a `to_dict`?" has an answer
that does not need looking up. Types that leave the process serialize:
`ACPEvent` into a stream, `ACPResult` into LangGraph state, `ACPSessionBinding`
into a store. Types that only configure a node -- `ACPSession`, `ACPWorkspace`,
`ACPConfig`, `ACPRequirements` -- do not: they are written in Python beside the
graph, and a graph definition is code rather than data.
"""

from langgraph_acp._json import JSONObject, JSONValue
from langgraph_acp.config import ACPConfig, ACPRequirements, UnsupportedOption
from langgraph_acp.errors import ACPAgentCapabilityError, ACPError, ACPSessionError
from langgraph_acp.events import EVENT_NAMESPACE, ACPEvent, ACPEventType
from langgraph_acp.result import ACPResult, ACPUsage
from langgraph_acp.session import (
    ACPSession,
    ACPSessionBinding,
    ACPSessionRef,
    ACPSessionStrategy,
)
from langgraph_acp.workspace import ACPWorkspace

__all__ = [
    "ACPAgentCapabilityError",
    "ACPConfig",
    "ACPError",
    "ACPEvent",
    "ACPEventType",
    "ACPRequirements",
    "ACPResult",
    "ACPSession",
    "ACPSessionBinding",
    "ACPSessionError",
    "ACPSessionRef",
    "ACPSessionStrategy",
    "ACPUsage",
    "ACPWorkspace",
    "EVENT_NAMESPACE",
    "JSONObject",
    "JSONValue",
    "UnsupportedOption",
]
