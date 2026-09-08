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

What it is *not* is a node. LangGraph can fan several tasks into one node, so
`review` may be three executions at once, and a registry keyed by node would
have kept the last of them and silently dropped the other two. Everything here
is addressed by `ExecutionId`.
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
from engine.graph_runtime.identity import ActiveExecution, ExecutionId
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
    """What is in flight for a run, for as long as it is.

    Keyed by `ExecutionId` within a run, so a superstep that fans two tasks into
    one node registers two entries rather than one overwriting the other. Order
    is insertion order, which is the order the frontier was started in and the
    order a client is shown.
    """

    def __init__(self) -> None:
        self._active: dict[
            RunId, dict[ExecutionId, tuple[NodeId, ControllableExecution]]
        ] = {}

    @contextmanager
    def in_flight(
        self,
        run_id: RunId,
        execution_id: ExecutionId,
        node_id: NodeId,
        execution: ControllableExecution,
    ) -> Iterator[ControllableExecution]:
        """Register for the length of the block, and release however it ends.

        A context manager rather than a register/release pair because the ways
        an execution stops are not all returns: it can raise, and it can be
        cancelled by a resume. An entry left behind by either would take
        steering meant for the run that replaced it.
        """
        self.register(run_id, execution_id, node_id, execution)
        try:
            yield execution
        finally:
            self.release(run_id, execution_id)

    def register(
        self,
        run_id: RunId,
        execution_id: ExecutionId,
        node_id: NodeId,
        execution: ControllableExecution,
    ) -> None:
        """Put an execution in flight until something releases it.

        The pair `in_flight` is written in terms of, for the bindings whose
        executions do not begin and end inside one block: a runtime that learns
        about a task from a stream registers it there and releases it when the
        stream says it ended, and has no `with` to hang either on.
        """
        self._active.setdefault(run_id, {})[execution_id] = (node_id, execution)

    def release(self, run_id: RunId, execution_id: ExecutionId) -> None:
        by_id = self._active.get(run_id)
        if by_id is None:
            return
        by_id.pop(execution_id, None)
        if not by_id:
            self._active.pop(run_id, None)

    def active(self, run_id: RunId) -> tuple[ActiveExecution, ...]:
        """Everything in flight for this run, each with the node it is running."""
        return tuple(
            ActiveExecution(execution_id, node_id)
            for execution_id, (node_id, _) in self._active.get(run_id, {}).items()
        )

    def resolve(
        self,
        run_id: RunId,
        execution_id: ExecutionId | None = None,
        node_id: NodeId | None = None,
    ) -> tuple[ActiveExecution, ControllableExecution]:
        """The execution a message is for, and which one it turned out to be.

        The identity travels back with it because the caller has to say where
        the message went -- an event that only reported "somebody was steered"
        is unreadable once a run has three agents in it.

        `execution_id` is the address; `node_id` is shorthand, and is only an
        answer while exactly one execution of that node is in flight. Raises
        `RunNotSteerableError` when nothing matches, and
        `AmbiguousExecutionError` when several do -- rather than picking one,
        which would be a guess about which agent the person was watching.
        """
        by_id = self._active.get(run_id, {})
        if not by_id:
            raise RunNotSteerableError("this run has no execution in flight")
        if execution_id is not None:
            found = by_id.get(execution_id)
            if found is None:
                raise RunNotSteerableError(f"{execution_id} is not executing")
            return ActiveExecution(execution_id, found[0]), found[1]
        matching = [
            (candidate, node, execution)
            for candidate, (node, execution) in by_id.items()
            if node_id is None or node == node_id
        ]
        if not matching:
            raise RunNotSteerableError(f"{node_id} is not executing")
        if len(matching) > 1:
            running = ", ".join(
                f"{candidate} ({node})" for candidate, node, _ in matching
            )
            raise AmbiguousExecutionError(
                f"name the execution to control: {running} are executing"
            )
        candidate, node, execution = matching[0]
        return ActiveExecution(candidate, node), execution


__all__ = ["ControllableExecution", "ExecutionRegistry"]
