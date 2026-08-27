"""ACP-compatible agents as first-class LangGraph nodes.

The boundary this package exists to hold:

    LangGraph owns orchestration and workflow durability.
    ACP owns the agent conversation and agent-side session state.
    langgraph-acp owns the binding between the two.

What is here so far is the vocabulary and the connection beneath it: the types a
node configures itself with, the events it streams, the result it returns, the
failures it reports, a provider that can launch an ACP agent and hold a live
session with it, and the store that remembers which conversation belongs to
which node. LangGraph itself has not arrived -- `ACPNode` is a later ticket --
so nothing yet joins the two halves.

    ACPAgentRegistry -> ACPAgentProvider -> ACPClient -> ACPSession
         "codex"          how to reach      one live     one conversation
                             an agent       connection      and its turns

    ACPSessionStore     (thread_id, session_key) -> "sess_abc123"
                        the binding, and only ever the binding

Naming an agent is confined to `langgraph_acp.providers`. Everything else --
every core type, the registry, the client, the session -- is written without
knowing whether it is talking to Codex or to something released next year, and
`"codex"` is a string a registry resolves rather than an import anyone makes.

Serialization follows one rule, so "does this have a `to_dict`?" has an answer
that does not need looking up. Types that leave the process serialize:
`ACPEvent` into a stream, `ACPResult` into LangGraph state. Types that only
configure a node -- `ACPSessionSpec`, `ACPWorkspace`, `ACPConfig`,
`ACPRequirements` -- do not: they are written in Python beside the graph, and a
graph definition is code rather than data.

Every field is annotated with what the constructor *accepts*, not with what it
stores: `__post_init__` normalizes, so a `Path` becomes a `str`, a list becomes
a tuple, and `"reuse"` becomes `ACPSessionStrategy.REUSE`. The stored value is
always an instance of the declared type but usually a narrower one. A stdlib
dataclass has no way to declare the two separately, and since `py.typed` ships
here, the spelling a caller writes is the one the annotation has to admit.

Containers handed in are copied, and so are containers handed back out by
`to_dict`. The copy is deep, because ACP payloads are nested and a shallow one
would leave the interesting part shared.
"""

from langgraph_acp._json import JSONObject, JSONValue
from langgraph_acp.agent import (
    ACPAgentProvider,
    ACPAgentRegistry,
    StdioACPProvider,
    default_registry,
)
from langgraph_acp.client import PROTOCOL_VERSION, ACPCapabilities, ACPClient
from langgraph_acp.config import ACPConfig, ACPRequirements, UnsupportedOption
from langgraph_acp.errors import (
    ACPAgentCapabilityError,
    ACPAgentNotFoundError,
    ACPConnectionError,
    ACPError,
    ACPSessionError,
)
from langgraph_acp.events import EVENT_NAMESPACE, ACPEvent, ACPEventType
from langgraph_acp.providers.codex import CODEX_ACP_COMMAND, CodexACPProvider
from langgraph_acp.result import ACPResult, ACPUsage
from langgraph_acp.session import (
    ACPPrompt,
    ACPSession,
    ACPSessionSpec,
    ACPSessionStrategy,
)
from langgraph_acp.store import ACPSessionStore, InMemoryACPSessionStore
from langgraph_acp.workspace import ACPWorkspace

__all__ = [
    "ACPAgentCapabilityError",
    "ACPAgentNotFoundError",
    "ACPAgentProvider",
    "ACPAgentRegistry",
    "ACPCapabilities",
    "ACPClient",
    "ACPConfig",
    "ACPConnectionError",
    "ACPError",
    "ACPEvent",
    "ACPEventType",
    "ACPPrompt",
    "ACPRequirements",
    "ACPResult",
    "ACPSession",
    "ACPSessionError",
    "ACPSessionSpec",
    "ACPSessionStore",
    "ACPSessionStrategy",
    "ACPUsage",
    "ACPWorkspace",
    "CODEX_ACP_COMMAND",
    "CodexACPProvider",
    "EVENT_NAMESPACE",
    "InMemoryACPSessionStore",
    "JSONObject",
    "JSONValue",
    "PROTOCOL_VERSION",
    "StdioACPProvider",
    "UnsupportedOption",
    "default_registry",
]
