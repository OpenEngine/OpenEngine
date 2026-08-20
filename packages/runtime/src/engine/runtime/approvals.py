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

An `accept_for_session` decision leaves a `SessionGrant` behind, and that is
what makes the next turn's identical request answerable without asking again --
the provider subprocess that was told about it will not be alive to remember.
The rule for reusing one is in `engine.runtime.session_grants` and is
deliberately narrow; what matters here is that a request answered from a grant
is still a persisted request with a recorded decision, distinguished only by
`decision_source`.

The configured policy is the other way a request is answered without a person,
and it is applied here for the same reason: this is the one place every paused
turn passes through, whichever runner paused it. `engine.runtime.approval_policy`
decides, this decides what to do with that -- and a policy decision is recorded
exactly as a human one is, because "who allowed this?" has to stay answerable
for the requests nobody was shown.
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
    SessionGrant,
)
from engine.domain.ids import AgentInstanceId, AgentRunId, ApprovalId, WorkspaceId
from engine.ports.agent_runner import ApprovalHandler, ApprovalRequest
from engine.ports.permissions import PermissionTranslator
from engine.ports.state_store import StateStore
from engine.runtime.approval_policy import PolicyDecision, policy_decision_for
from engine.runtime.config import ApprovalConfig
from engine.runtime.session_grants import matching_grant, session_grant_from

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

    `policy` is the deployment's configuration, and the default is the built-in
    one: nothing is preapproved except reads, so a caller that composes a broker
    without saying otherwise gets the behaviour of asking.
    """

    def __init__(
        self, store: StateStore, policy: ApprovalConfig = ApprovalConfig()
    ) -> None:
        self._store = store
        self._policy = policy
        self._waiting: dict[ApprovalId, asyncio.Future[ApprovalDecision]] = {}
        self._pending_requests: dict[
            ApprovalId,
            tuple[ApprovalRequest, PermissionTranslator | None, ApprovalPresenter],
        ] = {}

    def handler(
        self,
        *,
        agent_run_id: AgentRunId,
        instance_id: AgentInstanceId,
        runner: str,
        present: ApprovalPresenter,
        workspace_id: WorkspaceId | None = None,
        translator: PermissionTranslator | None = None,
        auto_approve: Callable[[], bool] | None = None,
    ) -> ApprovalHandler:
        """The callback one interactive turn hands to its runner.

        Bound to the run rather than to the conversation because that is what a
        decision has to match: an answer to a question asked by a process that
        has since been replaced is not an answer at all.

        `workspace_id` is where this turn is working, and bounds any consent it
        collects: a grant is a statement about one worktree.

        `translator` is the runner's own reading of its provider's requests, and
        without one the policy has nothing to evaluate: every request is
        unclassified, and every request is put to a person.
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
                tool_call_id=request.tool_call_id,
                workspace_id=workspace_id,
                arguments=request.arguments,
                allowed_decisions=tuple(request.allowed_decisions),
            )

            # Both of these are looked up before the request is written, so no
            # reader can ever see a pending row for a question that was never
            # going to be put. Policy first: a deployment that has ruled on
            # this has ruled on it whatever the conversation agreed to earlier.
            configured = self._policy_decision(
                request,
                translator,
                auto_approve=bool(auto_approve and auto_approve()),
            )
            if configured is not None:
                settled = replace(
                    record,
                    status=ApprovalStatus.DECIDED,
                    decision=configured,
                    decision_source=ApprovalDecisionSource.POLICY,
                    decided_at=datetime.now(UTC),
                )
                await self._store.record_approval(settled)
                # Presented for the same reason a grant's is: the client says
                # what ran on its behalf, and nothing waits on it.
                await present(settled)
                return configured

            granted = await self._matching_grant(record)
            if granted is not None:
                # Answered by something the user already said. It is still a
                # request and still recorded as one -- with the decision, and
                # with `session_grant` as the reason it holds -- because "what
                # was this agent allowed to do, and on whose say-so" has to
                # stay answerable for the ones nobody was shown.
                decided = replace(
                    record,
                    status=ApprovalStatus.DECIDED,
                    decision=ApprovalDecision.ACCEPT_FOR_SESSION,
                    decision_source=ApprovalDecisionSource.SESSION_GRANT,
                    decided_at=datetime.now(UTC),
                )
                await self._store.record_approval(decided)
                # Presented so the client can say what was allowed on its
                # behalf. Nothing waits on it: a resolved request is not a
                # prompt, and there is no card to answer.
                await present(decided)
                return ApprovalDecision.ACCEPT_FOR_SESSION

            await self._store.record_approval(record)
            waiting: asyncio.Future[ApprovalDecision] = (
                asyncio.get_running_loop().create_future()
            )
            self._waiting[record.approval_id] = waiting
            self._pending_requests[record.approval_id] = (request, translator, present)
            try:
                # The conversation setting can change while the durable row is
                # being written. Recheck after the future exists so a toggle in
                # that window cannot leave the turn parked on a request the
                # system now allows.
                configured = self._policy_decision(
                    request,
                    translator,
                    auto_approve=bool(auto_approve and auto_approve()),
                )
                if configured is not None:
                    settled = await self.decide(
                        record.approval_id,
                        configured,
                        instance_id=instance_id,
                        agent_run_id=agent_run_id,
                        source=ApprovalDecisionSource.POLICY,
                    )
                    await present(settled)
                    return configured
                await present(record)
                return await waiting
            except BaseException:
                # Cancelled, or a presenter that failed. Either way nothing is
                # listening any more, and the request must not be left open.
                await self._interrupt(record.approval_id)
                raise
            finally:
                self._waiting.pop(record.approval_id, None)
                self._pending_requests.pop(record.approval_id, None)

        return request_approval

    async def auto_approve_pending(
        self, instance_id: AgentInstanceId
    ) -> Sequence[ApprovalRecord]:
        """Apply auto-approval to requests already waiting in one conversation.

        Explicit ask and deny rules retain their configured precedence. This is
        used when an implementation conversation enables auto-approval after
        its workflow runner has already paused.
        """

        settled: list[ApprovalRecord] = []
        for record in await self._store.list_approvals(
            instance_id=instance_id, status=ApprovalStatus.PENDING
        ):
            context = self._pending_requests.get(record.approval_id)
            if context is None:
                continue
            request, translator, present = context
            decision = self._policy_decision(
                request, translator, auto_approve=True
            )
            if decision is None:
                continue
            try:
                decided = await self.decide(
                    record.approval_id,
                    decision,
                    instance_id=instance_id,
                    agent_run_id=record.agent_run_id,
                    source=ApprovalDecisionSource.POLICY,
                )
            except ApprovalError:
                continue
            await present(decided)
            settled.append(decided)
        return tuple(settled)

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
        grant = (
            session_grant_from(record)
            if decision is ApprovalDecision.ACCEPT_FOR_SESSION
            and source is ApprovalDecisionSource.USER
            else None
        )
        try:
            if grant is not None:
                # Before the decision, so a failure here leaves the request
                # unanswered and re-answerable rather than half-granted. The
                # grant is only reachable through a later request, and there
                # cannot be one until this turn is let go again.
                await self._store.record_session_grant(grant)
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

    def _policy_decision(
        self,
        request: ApprovalRequest,
        translator: PermissionTranslator | None,
        *,
        auto_approve: bool = False,
    ) -> ApprovalDecision | None:
        """The answer the configuration already gives, if it gives one.

        `None` is "put this to a person", and covers two cases that end the same
        way. The policy may say to ask. Or it may call for a decision this
        particular request was never offered -- a provider that is not offering
        a cancellation has no way to honour one, and answering with a decision
        it never offered would be a protocol error rather than an enforcement.
        """
        scope = translator.scope_for(request) if translator is not None else None
        policy = replace(self._policy, auto_approve=True) if auto_approve else self._policy
        match policy_decision_for(policy, scope):
            case PolicyDecision.ALLOW:
                configured = ApprovalDecision.ACCEPT
            case PolicyDecision.DENY:
                configured = ApprovalDecision.CANCEL
            case _:
                return None
        return configured if configured in request.allowed_decisions else None

    async def _matching_grant(self, record: ApprovalRecord) -> SessionGrant | None:
        """The consent this conversation already gave for exactly this action.

        Scoped by the store query to the conversation, and then by
        `matching_grant` on every other axis. Read fresh each time rather than
        cached: the grants are what the *conversation* has agreed to, and this
        process is one of several that may be writing them.
        """
        return matching_grant(
            record,
            await self._store.list_session_grants(instance_id=record.instance_id),
        )

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
