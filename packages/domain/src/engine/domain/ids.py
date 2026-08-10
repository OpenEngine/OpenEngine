"""Identifier types.

`NewType` rather than bare `str` so a `TaskId` cannot silently be passed where a
`RunId` is expected. No runtime cost.
"""

from typing import NewType

TaskId = NewType("TaskId", str)
"""A unit of work requested of the engine, e.g. "fix the flaky auth test"."""

RunId = NewType("RunId", str)
"""One end-to-end execution of a `TaskId`."""

AttemptId = NewType("AttemptId", str)
"""One agent attempt within a run. A run may retry, producing several attempts."""

WorkspaceId = NewType("WorkspaceId", str)
"""A checked-out, isolated filesystem an agent works in."""

AgentId = NewType("AgentId", str)
"""One agent instance -- a planner or a worker. See `engine.ports.AgentSpec`."""

PlanId = NewType("PlanId", str)
"""A plan the planner is working from."""

__all__ = ["AgentId", "AttemptId", "PlanId", "RunId", "TaskId", "WorkspaceId"]
