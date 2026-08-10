"""Identifier types.

`NewType` rather than bare `str` so a `TaskId` cannot silently be passed where a
`RunId` is expected. No runtime cost.

Three of these spell out the agent identity model, innermost first:

    AgentId          the logical role      ("foreman", "coder")
      -> AgentInstanceId   a durable instance of that role, owning a conversation
        -> AgentRunId      one execution of that instance

That layering is what lets an agent stop for clarification and later continue as
the same logical instance -- see `engine.domain.agents`.
"""

from typing import NewType

TaskId = NewType("TaskId", str)
"""A unit of work requested of the engine, e.g. "fix the flaky auth test"."""

RunId = NewType("RunId", str)
"""One end-to-end execution of a `TaskId`."""

AgentId = NewType("AgentId", str)
"""A logical agent role, naming an `AgentProfile`: "foreman", "coder"."""

AgentInstanceId = NewType("AgentInstanceId", str)
"""A durable instance of an agent role. Owns one conversation."""

AgentRunId = NewType("AgentRunId", str)
"""One execution of an `AgentInstanceId`. An instance may run many times."""

ConversationId = NewType("ConversationId", str)
"""The message history belonging to one agent instance."""

MessageId = NewType("MessageId", str)
"""One stored message within a conversation."""

WorkspaceId = NewType("WorkspaceId", str)
"""A checked-out, isolated filesystem an agent works in."""

__all__ = [
    "AgentId",
    "AgentInstanceId",
    "AgentRunId",
    "ConversationId",
    "MessageId",
    "RunId",
    "TaskId",
    "WorkspaceId",
]
