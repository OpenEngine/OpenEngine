"""The ACP agents this package knows how to reach out of the box.

One module per agent implementation, and nothing above this package may import
one: an adapter that names an agent is an adapter that works with that agent.
Everything here is an `ACPAgentProvider`, which is to say a command line and
whatever compatibility behaviour that particular CLI turns out to need.
"""

from langgraph_acp.providers.claude import CLAUDE_ACP_COMMAND, ClaudeACPProvider
from langgraph_acp.providers.codex import CODEX_ACP_COMMAND, CodexACPProvider

__all__ = [
    "CLAUDE_ACP_COMMAND",
    "CODEX_ACP_COMMAND",
    "ClaudeACPProvider",
    "CodexACPProvider",
]
