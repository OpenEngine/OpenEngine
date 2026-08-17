"""Brokering consent between a paused provider turn and whoever answers it.

An interactive runner stops mid-turn and awaits a decision. That await is a
coroutine inside one process, but the question it represents is asked of a
person over HTTP, on a connection that may not be the one that started the run
and may not still exist. This is the piece in between: it turns the runner's
callback into a durable record, hands it to whoever is presenting it, and parks
the turn on a future until a decision arrives through a different request
entirely.

Two orderings are the whole point and are not negotiable:

* **Persist, then present.** A request is durable before anybody can see it, so
  a decision can never arrive for something no reader could later explain.
* **Persist, then resume.** A decision is durable before the provider is told,
  so a crash between the two cannot leave a command that was run according to
  the provider and never approved according to us.

Everything else here exists so that a pending request cannot outlive the thing
that was waiting for it. A future nobody will ever resolve is a turn that hangs
forever; a `pending` row nobody will ever answer is a prompt the UI shows for a
process that died last Tuesday. So every way a turn can end -- decided,
cancelled, crashed, restarted into -- resolves both.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from engine.domain.approvals import (
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalRecord,
    ApprovalStatus,
)
from engine.domain.ids import AgentInstanceId, AgentRunId, ApprovalId
from engine.ports.agent_runner import ApprovalHandler, ApprovalRequest
from engine.ports.state_store import StateStore

ApprovalPresenter = Callable[[ApprovalRecord], Awaitable[None]]
"""Shows a persisted request to whoever can answer it, and returns.

It does not wait for the decision -- the broker does that. Presenting is
publishing a snapshot: an HTTP stream, a queue, a test's list.
"""


class ApprovalError(RuntimeError):
    """Something is wrong with a decision. The subclasses say what."""


class UnknownApprovalError(ApprovalError):
    """No such request, or not one this conversation is entitled to answer.

    Deliberately the same error for both: a caller holding an id from another
    conversation learns nothing about whether it exists.
    """


class ApprovalNotPendingError(ApprovalError):
    """The request is no longer open to a decision.

    Already decided, interrupted by a restart or a failure, or belonging to a
    run that has since ended. All the same to the caller: whatever was waiting
    for this answer is not waiting any more.
    """


class ApprovalDecisionNotAllowedError(ApprovalError):
    """A decision the provider never offered for this request.

    Refused rather than translated into the nearest thing: a provider that did
    not offer a session grant has no way to honour one, and quietly downgrading
    it to a single acceptance would be a permission decision made by us on the
    user's behalf.
    """


class ApprovalsUnsupportedError(ApprovalError):
    """An approval callback was wired to a runner that cannot pause.

    Ignoring it would be worse than failing: the caller believes commands are
    being gated on a human, and the runner would be running them unattended.
    """

    def __init__(self, runner: str) -> None:
        super().__init__(
            f"runner {runner!r} cannot pause for approval; select an interactive "
            "runner or omit the approval callback"
        )
        self.runner = runner


class ApprovalBroker:
    """Durable approval requests, and the futures paused turns wait on.

    One per process, shared by every conversation: the futures are what make it
    stateful, and they belong to the process running the provider subprocesses
    rather than to any one conversation.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._waiting: dict[ApprovalId, asyncio.Future[ApprovalDecision]] = {}

    def handler(
        self,
        *,
        agent_run_id: AgentRunId,
        instance_id: AgentInstanceId,
        runner: str,
        present: ApprovalPresenter,
    ) -> ApprovalHandler:
        """The callback one interactive turn hands to its runner.

        Bound to the run rather than to the conversation because that is what a
        decision has to match: an answer to a question asked by a process that
        has since been replaced is not an answer at all.
        """

        async def request_approval(request: ApprovalRequest) -> ApprovalDecision:
            record = ApprovalRecord(
                approval_id=_new_approval_id(),
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                runner=runner,
                kind=request.kind,
                requested_at=datetime.now(UTC),
                reason=request.reason,
                command=request.command,
                cwd=request.cwd,
                tool_name=request.tool_name,
                arguments=request.arguments,
                allowed_decisions=tuple(request.allowed_decisions),
            )
            waiting: asyncio.Future[ApprovalDecision] = (
                asyncio.get_running_loop().create_future()
            )
            await self._store.record_approval(record)
            self._waiting[record.approval_id] = waiting
            try:
                await present(record)
                return await waiting
            except BaseException:
                # Cancelled, or a presenter that failed. Either way nothing is
                # listening any more, and the request must not be left open.
                await self._interrupt(record.approval_id)
                raise
            finally:
                self._waiting.pop(record.approval_id, None)

        return request_approval

    async def decide(
        self,
        approval_id: ApprovalId,
        decision: ApprovalDecision,
        *,
        instance_id: AgentInstanceId,
        agent_run_id: AgentRunId | None,
        source: ApprovalDecisionSource = ApprovalDecisionSource.USER,
    ) -> ApprovalRecord:
        """Answer one request, and resume the turn that is waiting for it.

        `agent_run_id` is the run the caller believes is current; `None` means
        the conversation has none. Both are checked rather than trusted, so a
        decision aimed at a run that has already ended fails instead of being
        applied to whatever is running now.
        """
        record = await self._store.load_approval(approval_id)
        if record is None or record.instance_id != instance_id:
            raise UnknownApprovalError(f"no approval {approval_id} in this conversation")
        if not record.is_pending:
            raise ApprovalNotPendingError(
                f"approval {approval_id} is already {record.status.value}"
            )
        if agent_run_id is None or record.agent_run_id != agent_run_id:
            raise ApprovalNotPendingError(
                f"approval {approval_id} belongs to a run that is no longer active"
            )
        if decision not in record.allowed_decisions:
            allowed = ", ".join(value.value for value in record.allowed_decisions)
            raise ApprovalDecisionNotAllowedError(
                f"{decision.value!r} is not one of the decisions offered for "
                f"{approval_id}: {allowed}"
            )

        # Claiming the future is what makes a duplicate decision lose rather
        # than double-resolve: the pop is the last thing before any await, so
        # exactly one caller can be holding it.
        waiting = self._waiting.pop(approval_id, None)
        if waiting is None or waiting.done():
            raise ApprovalNotPendingError(
                f"nothing is waiting for approval {approval_id}"
            )

        decided = replace(
            record,
            status=ApprovalStatus.DECIDED,
            decision=decision,
            decision_source=source,
            decided_at=datetime.now(UTC),
        )
        try:
            await self._store.record_approval(decided)
        except Exception:
            # The turn is still paused and the decision never happened, so put
            # the request back rather than stranding it.
            self._waiting[approval_id] = waiting
            raise
        if not waiting.done():
            # It can only be done if the turn was torn down while the decision
            # was being written. The decision still happened and still stands;
            # there is simply nothing left to hand it to.
            waiting.set_result(decision)
        return decided

    async def cancel_run(self, agent_run_id: AgentRunId) -> Sequence[ApprovalRecord]:
        """Resolve everything this run is waiting on, because it is being stopped.

        Cancelling is a decision, so it is recorded as one and handed to the
        provider -- which is how the denied action does not run. A request that
        cannot take a cancellation is interrupted instead: stopping a run must
        not be able to leave a pending approval behind whatever the provider
        offered.
        """
        resolved: list[ApprovalRecord] = []
        for record in await self._store.list_approvals(
            agent_run_id=agent_run_id, status=ApprovalStatus.PENDING
        ):
            try:
                resolved.append(
                    await self.decide(
                        record.approval_id,
                        ApprovalDecision.CANCEL,
                        instance_id=record.instance_id,
                        agent_run_id=agent_run_id,
                    )
                )
            except ApprovalError:
                interrupted = await self._interrupt(record.approval_id)
                if interrupted is not None:
                    resolved.append(interrupted)
        return tuple(resolved)

    async def interrupt_run(self, agent_run_id: AgentRunId) -> Sequence[ApprovalRecord]:
        """Close out a run's requests because the turn ended without answering.

        The provider failed, the protocol broke, or the turn was torn down. No
        decision was made and none can be, so the requests end `interrupted`
        rather than looking like a cancellation somebody chose.
        """
        return await self._interrupt_all(
            await self._store.list_approvals(
                agent_run_id=agent_run_id, status=ApprovalStatus.PENDING
            )
        )

    async def interrupt_orphans(self) -> Sequence[ApprovalRecord]:
        """Close out requests left pending by a process that is gone.

        Called once at startup. A CLI subprocess dies with the server that
        spawned it, so a `pending` row nobody in this process is waiting on
        describes a question that can no longer be put to anything -- accepting
        it would resume nothing.
        """
        return await self._interrupt_all(
            [
                record
                for record in await self._store.list_approvals(
                    status=ApprovalStatus.PENDING
                )
                if record.approval_id not in self._waiting
            ]
        )

    def is_waiting(self, approval_id: ApprovalId) -> bool:
        """Whether a turn in this process is still parked on that request."""
        waiting = self._waiting.get(approval_id)
        return waiting is not None and not waiting.done()

    async def _interrupt_all(
        self, records: Sequence[ApprovalRecord]
    ) -> Sequence[ApprovalRecord]:
        interrupted = [await self._interrupt(record.approval_id) for record in records]
        return tuple(record for record in interrupted if record is not None)

    async def _interrupt(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        waiting = self._waiting.pop(approval_id, None)
        record = await self._store.load_approval(approval_id)
        if record is None or not record.is_pending:
            return record
        interrupted = replace(record, status=ApprovalStatus.INTERRUPTED)
        await self._store.record_approval(interrupted)
        if waiting is not None and not waiting.done():
            # Whoever is still parked on this learns the same way a cancelled
            # turn does, rather than waiting for an answer that cannot come.
            waiting.cancel()
        return interrupted


def _new_approval_id() -> ApprovalId:
    """Mint an id for one request.

    Ours rather than the provider's: Codex numbers requests within a thread and
    Claude within a session, so neither is unique across the runs of one
    conversation, and an id that repeats is an id a stale decision can hit.
    """
    return ApprovalId(f"apv-{uuid4().hex[:12]}")


__all__ = [
    "ApprovalBroker",
    "ApprovalDecisionNotAllowedError",
    "ApprovalError",
    "ApprovalNotPendingError",
    "ApprovalPresenter",
    "ApprovalsUnsupportedError",
    "UnknownApprovalError",
]
