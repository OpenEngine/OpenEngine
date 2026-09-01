"""Routing external control to whatever is executing now.

Steering and execution-level approvals do not belong to the graph. They belong
to the thing a node is driving -- for an agent node, an ACP session talking to
Claude or Codex, which stays alive for the whole node: while it works, while it
waits for permission to run a command, and while somebody redirects it. The
graph's job is deciding what runs next, and a message for an agent that is
already running is not that question.

So a message is a lookup here followed by a hand-off, and the graph node is
never suspended and resumed to carry one. Doing it the other way round -- an
interrupt per approval, a resume per instruction -- would tear down the session
mid-turn and rebuild it, losing the conversation the agent was in the middle of
and turning "keep going, but rename the flag" into "start again, knowing one
more thing".

## Two kinds of human-in-the-loop, and only one of them is here

**Execution-level.** The agent wants to run a command; the agent wants to use a
tool; a person wants to redirect the agent it is watching. These are questions
about the turn in progress. They go to the execution, through `steer()` and
`decide()`, and the graph does not observe them except as events.

**Workflow-level.** Approve a deployment; accept a milestone; choose a branch;
send an implementation back for revision. These are questions about what the
graph should do, they outlive any session, and they are what a graph interrupt
and a checkpoint are for -- `resume_from` is the one in this package.

Mixing them is the failure mode this split exists to prevent: an execution-level
approval routed through a graph interrupt stops the whole workflow to ask
whether one agent may run `pytest`.

## What an execution is

Nothing here knows. `ControllableExecution` is two methods; an agent node
registers one backed by its session, a test registers one backed by a script,
and this package imports neither. A generic runtime that reached for ACP,
Claude or Codex would be a control surface that only works for the agents it
was written against.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

from engine.domain import ApprovalDecision, ApprovalId, RunId

from engine.graph_runtime.control import (
    AmbiguousExecutionError,
    RunNotSteerableError,
)
from engine.graph_runtime.topology import NodeId


@runtime_checkable
class ControllableExecution(Protocol):
    """Something in flight that can be told things while it runs."""

    async def steer(self, message: str) -> None:
        """Take an instruction now, without being restarted to receive it."""
        ...

    async def decide(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        """Take the answer to a request this execution raised."""
        ...


class ExecutionRegistry:
    """Which execution is in flight for which node, for as long as it is.

    Keyed by node within a run, because a superstep is plural: three reviewers
    are three registered executions at once, and a message has to be able to
    name which one it is for.
    """

    def __init__(self) -> None:
        self._active: dict[RunId, dict[NodeId, ControllableExecution]] = {}

    @contextmanager
    def in_flight(
        self, run_id: RunId, node_id: NodeId, execution: ControllableExecution
    ) -> Iterator[ControllableExecution]:
        """Register for the length of the block, and release however it ends.

        A context manager rather than a register/release pair because the ways
        a node stops are not all returns: it can raise, and it can be cancelled
        by a resume. An entry left behind by either would take steering meant
        for the run that replaced it.
        """
        self._active.setdefault(run_id, {})[node_id] = execution
        try:
            yield execution
        finally:
            self.release(run_id, node_id)

    def release(self, run_id: RunId, node_id: NodeId) -> None:
        by_node = self._active.get(run_id)
        if by_node is None:
            return
        by_node.pop(node_id, None)
        if not by_node:
            self._active.pop(run_id, None)

    def active(self, run_id: RunId) -> tuple[NodeId, ...]:
        """The nodes with something in flight, which is a run's frontier."""
        return tuple(self._active.get(run_id, {}))

    def resolve(
        self, run_id: RunId, node_id: NodeId | None = None
    ) -> tuple[NodeId, ControllableExecution]:
        """The execution a message is for, and the node it belongs to.

        The node travels back with it because the caller has to say where the
        message went -- an event that only reported "somebody was steered" is
        unreadable once a run has three agents in it.

        Raises `RunNotSteerableError` when there is none, and
        `AmbiguousExecutionError` when there are several and none was named --
        rather than picking one, which would be a guess about which agent the
        person was watching.
        """
        by_node = self._active.get(run_id, {})
        if not by_node:
            raise RunNotSteerableError("this run has no execution in flight")
        if node_id is None:
            if len(by_node) > 1:
                running = ", ".join(sorted(str(node) for node in by_node))
                raise AmbiguousExecutionError(
                    f"name the node to control: {running} are executing"
                )
            return next(iter(by_node.items()))
        execution = by_node.get(node_id)
        if execution is None:
            raise RunNotSteerableError(f"{node_id} is not executing")
        return node_id, execution


__all__ = ["ControllableExecution", "ExecutionRegistry"]
