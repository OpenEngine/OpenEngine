"""Pausing a turn on a person, and resuming the same provider process.

An approval is the one place where a running agent waits on something outside
the process for an unbounded time, so the interesting cases are all about what
happens to that wait when something else goes wrong: the browser leaves, the
run is stopped, the CLI dies, the server restarts. Each of those has a test
here, because each of them can strand a turn nobody will ever finish or leave a
prompt nobody can ever answer.

The fake runners stand in for the two interactive adapters. They are shaped
like the real ones on the point that matters: the request is awaited inline in
the turn, so whatever happens to that await happens to the turn.
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import create_app
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalId,
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    Message,
)
from engine.ports import AgentTurn, ApprovalRequest, FinishReason, StateStore
from engine.runtime import (
    AgentSession,
    ApprovalBroker,
    ApprovalsUnsupportedError,
    Capabilities,
)

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(agent_id=CODER, instructions="Be terse.", description="Codes.")
}

RUN_TESTS = ApprovalRequest(
    approval_id="provider-1",
    kind=ApprovalKind.COMMAND_EXECUTION,
    reason="Run the test suite",
    command="pytest",
    cwd="/workspace",
)
WRITE_FILE = ApprovalRequest(
    approval_id="provider-2",
    kind=ApprovalKind.FILE_CHANGE,
    reason="Write the regression test",
    tool_name="Write",
    arguments='{"path":"tests/test_worker.py"}',
    allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
)


class ApprovalRunner:
    """An interactive runner that stops for consent before each action.

    Cancelling ends the turn the way both CLIs end it -- an error finish rather
    than an exception -- so a test asserting that the denied action never ran is
    asserting against the shape the real adapters produce.
    """

    def __init__(
        self,
        requests: Sequence[ApprovalRequest] = (RUN_TESTS,),
        reply: str = "All tests passed.",
    ) -> None:
        self.requests = tuple(requests)
        self.reply = reply
        self.decisions: list[ApprovalDecision] = []
        self.executed: list[str] = []
        self.runs: list[AgentRunId] = []

    async def run_turn(self, *args: object, **kwargs: object) -> AgentTurn:
        raise AssertionError("an interactive turn must not fall back to run_turn")

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.runs.append(agent_run_id)
        for request in self.requests:
            decision = await on_approval(request)
            self.decisions.append(decision)
            if decision is ApprovalDecision.CANCEL:
                message = Message.assistant("Stopped without running that.")
                if on_message is not None:
                    on_message(message)
                return AgentTurn(message, finish_reason=FinishReason.ERROR)
            self.executed.append(request.command or request.tool_name or "")
        message = Message.assistant(self.reply)
        if on_message is not None:
            on_message(message)
        return AgentTurn(message)

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


class DyingApprovalRunner(ApprovalRunner):
    """A provider whose transport breaks while a decision is outstanding."""

    def __init__(self) -> None:
        super().__init__()
        self.presented = asyncio.Event()

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.runs.append(agent_run_id)
        asking = asyncio.create_task(on_approval(RUN_TESTS))
        try:
            await self.presented.wait()
            raise RuntimeError("codex app-server exited 1")
        finally:
            # What the adapters do when they lose the process: the outstanding
            # request is unwound rather than left holding the turn open.
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)


class PlainRunner:
    """A runner with no interactive method at all, like `codex exec`."""

    def __init__(self) -> None:
        self.seen: list[tuple[Message, ...]] = []

    async def run_turn(
        self, agent_run_id, profile, messages, tools=(), workspace_id=None
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        return AgentTurn(Message.assistant("Answered without asking."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


def _session(store: StateStore, runner: object) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=unused,
            state_store=store,
        ),
        profiles=PROFILES,
        runners={"test": runner},
    )


def _app(store: StateStore, runner: object):
    return create_app(_session(store, runner), {"test": runner})


async def _thread(client: httpx.AsyncClient) -> str:
    created = await client.post(
        "/api/threads", json={"agentId": "coder", "runner": "test"}
    )
    return created.json()["id"]


async def _pending(store: StateStore, after: int = 0) -> ApprovalRecord:
    """Wait until the turn has asked its next question, and return it."""
    for _ in range(400):
        approvals = await store.list_approvals()
        if len(approvals) > after and approvals[-1].is_pending:
            return approvals[-1]
        await asyncio.sleep(0.005)
    raise AssertionError("the run never asked for approval")


async def _ignore(record: ApprovalRecord) -> None:
    """A presenter for tests that decide out of band."""


def _lines(response: httpx.Response) -> list[dict[str, object]]:
    return [json.loads(line) for line in response.text.splitlines() if line]


def _approval_url(thread_id: str, approval_id: str) -> str:
    return f"/api/threads/{thread_id}/runs/current/approvals/{approval_id}"


# --- the broker -------------------------------------------------------------


def test_a_request_is_durable_before_anybody_is_shown_it() -> None:
    """Otherwise a decision could arrive for something no reader can explain."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)
    when_presented: list[ApprovalRecord | None] = []

    async def scenario() -> ApprovalDecision:
        instance = await store.create_instance(CODER)
        presented: asyncio.Future = asyncio.get_running_loop().create_future()

        async def present(record: ApprovalRecord) -> None:
            # What the store already knows at the moment it becomes visible.
            when_presented.append(await store.load_approval(record.approval_id))
            presented.set_result(record)

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="test",
            present=present,
        )
        paused = asyncio.create_task(handler(RUN_TESTS))
        record = await presented
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-1"),
        )
        return await paused

    decision = asyncio.run(scenario())
    stored = when_presented[0]

    assert decision is ApprovalDecision.ACCEPT
    assert stored is not None
    assert stored.is_pending
    assert stored.command == "pytest"
    assert stored.kind is ApprovalKind.COMMAND_EXECUTION
    assert stored.allowed_decisions == tuple(ApprovalDecision)


def test_a_decision_is_durable_before_the_provider_is_told() -> None:
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)
    when_resumed: list[ApprovalRecord | None] = []

    async def scenario() -> None:
        instance = await store.create_instance(CODER)
        presented: asyncio.Future = asyncio.get_running_loop().create_future()

        async def present(record: ApprovalRecord) -> None:
            presented.set_result(record)

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="test",
            present=present,
        )

        async def paused_turn() -> ApprovalDecision:
            decision = await handler(RUN_TESTS)
            # What the store knows at the instant the turn is let go again.
            record = presented.result()
            when_resumed.append(await store.load_approval(record.approval_id))
            return decision

        turn = asyncio.create_task(paused_turn())
        record = await presented
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-1"),
        )
        assert await turn is ApprovalDecision.ACCEPT

    asyncio.run(scenario())
    decided = when_resumed[0]

    assert decided is not None
    assert decided.status is ApprovalStatus.DECIDED
    assert decided.decision is ApprovalDecision.ACCEPT
    assert decided.decision_source is ApprovalDecisionSource.USER
    assert decided.decided_at is not None


def test_a_run_that_ends_without_answering_interrupts_what_it_asked() -> None:
    """The backstop for a turn torn down while a question was outstanding."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario() -> Sequence[ApprovalRecord]:
        instance = await store.create_instance(CODER)
        await store.record_approval(
            ApprovalRecord(
                approval_id=ApprovalId("apv-orphan"),
                agent_run_id=AgentRunId("ar-1"),
                instance_id=instance.instance_id,
                runner="test",
                kind=ApprovalKind.COMMAND_EXECUTION,
                requested_at=datetime.now(UTC),
                allowed_decisions=tuple(ApprovalDecision),
            )
        )
        await broker.interrupt_run(AgentRunId("ar-1"))
        return await store.list_approvals()

    approvals = asyncio.run(scenario())

    assert [record.status for record in approvals] == [ApprovalStatus.INTERRUPTED]
    # Interrupted is not a decision somebody made, and does not pretend to be.
    assert approvals[0].decision is None


def test_an_approval_callback_on_a_runner_that_cannot_pause_is_refused() -> None:
    """Ignoring it would run unattended what the caller believes is gated."""
    store = InMemoryStateStore()
    runner = PlainRunner()
    session = _session(store, runner)

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        raise AssertionError("nothing should reach the handler")

    async def scenario() -> None:
        instance = await session.start(CODER)
        await session.say(instance.instance_id, "go", on_approval=approve)

    with pytest.raises(ApprovalsUnsupportedError):
        asyncio.run(scenario())
    assert runner.seen == []


def test_a_cancelled_turn_is_recorded_as_cancelled_not_failed() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    session = _session(store, runner)
    broker = ApprovalBroker(store)
    cancelled = AgentRunId("ar-cancelled")

    async def scenario():
        instance = await session.start(CODER)
        handler = broker.handler(
            agent_run_id=cancelled,
            instance_id=instance.instance_id,
            runner="test",
            present=_ignore,
        )
        turn = asyncio.create_task(
            session.say(
                instance.instance_id,
                "run the tests",
                on_approval=handler,
                agent_run_id=cancelled,
            )
        )
        request = await _pending(store)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        return (
            await store.agent_run(cancelled),
            await store.load_approval(request.approval_id),
            broker.is_waiting(request.approval_id),
        )

    agent_run, approval, waiting = asyncio.run(scenario())

    assert agent_run is not None
    assert agent_run.status is AgentRunStatus.CANCELLED
    assert approval is not None
    assert approval.status is ApprovalStatus.INTERRUPTED
    assert waiting is False
    assert runner.executed == []


# --- persistence ------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path) -> StateStore:
    """The two stores a running process uses, held to the same contract."""
    if request.param == "memory":
        return InMemoryStateStore()
    sqlite = SQLiteStateStore(tmp_path / "conversations.sqlite3")
    request.addfinalizer(sqlite.close)
    return sqlite


def test_stores_agree_on_what_an_approval_is(store: StateStore) -> None:
    async def scenario():
        instance = await store.create_instance(CODER, runner="claude-control")
        asked = ApprovalRecord(
            approval_id=ApprovalId("apv-1"),
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="claude-control",
            kind=ApprovalKind.COMMAND_EXECUTION,
            requested_at=datetime.now(UTC),
            reason="Run the test suite",
            command="pytest",
            cwd="/workspace",
            tool_name="Bash",
            arguments='{"command":"pytest"}',
            allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
        )
        later = replace(
            asked,
            approval_id=ApprovalId("apv-2"),
            agent_run_id=AgentRunId("ar-2"),
            kind=ApprovalKind.TOOL_USE,
            command=None,
            cwd=None,
            tool_name="WebFetch",
        )
        await store.record_approval(asked)
        await store.record_approval(later)
        decided = replace(
            asked,
            status=ApprovalStatus.DECIDED,
            decision=ApprovalDecision.CANCEL,
            decision_source=ApprovalDecisionSource.USER,
            decided_at=datetime.now(UTC),
        )
        await store.record_approval(decided)
        return (
            decided,
            await store.load_approval(ApprovalId("apv-1")),
            await store.load_approval(ApprovalId("apv-missing")),
            await store.list_approvals(),
            await store.list_approvals(agent_run_id=AgentRunId("ar-2")),
            await store.list_approvals(status=ApprovalStatus.PENDING),
            await store.list_approvals(instance_id=AgentInstanceId("agi-elsewhere")),
        )

    decided, loaded, missing, everything, by_run, pending, elsewhere = asyncio.run(
        scenario()
    )

    assert loaded == decided
    assert missing is None
    # Answering a request does not move it in the log of what was asked.
    assert [record.approval_id for record in everything] == ["apv-1", "apv-2"]
    assert [record.approval_id for record in by_run] == ["apv-2"]
    assert [record.approval_id for record in pending] == ["apv-2"]
    assert elsewhere == ()


def test_an_approval_needs_a_conversation_to_belong_to(store: StateStore) -> None:
    """An approval no reviewer could reach from a conversation is a leak."""
    orphan = ApprovalRecord(
        approval_id=ApprovalId("apv-1"),
        agent_run_id=AgentRunId("ar-1"),
        instance_id=AgentInstanceId("agi-nope"),
        runner="test",
        kind=ApprovalKind.TOOL_USE,
        requested_at=datetime.now(UTC),
    )

    with pytest.raises(KeyError):
        asyncio.run(store.record_approval(orphan))


# --- the HTTP surface -------------------------------------------------------


def test_approving_resumes_the_paused_turn_and_records_the_decision() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            decided = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "accept"},
            )
            streamed = await started
            history = await client.get(f"/api/threads/{thread_id}/messages")
            stored = await store.load_approval(request.approval_id)
            return request, decided, streamed, history, stored

    request, decided, streamed, history, stored = asyncio.run(scenario())
    events = _lines(streamed)
    approvals = [event for event in events if event["type"] == "approval"]

    assert request.agent_run_id == runner.runs[0]
    assert request.runner == "test"
    assert decided.status_code == 200
    assert decided.json()["approval"]["status"] == "decided"
    assert decided.json()["approval"]["decision"] == "accept"
    # The whole request reaches the stream, before any decision was made.
    assert approvals[0]["approval"] == {
        "id": str(request.approval_id),
        "status": "pending",
        "kind": "command_execution",
        "reason": "Run the test suite",
        "command": "pytest",
        "cwd": "/workspace",
        "toolName": None,
        "arguments": None,
        "allowedDecisions": ["accept", "accept_for_session", "cancel"],
        "decision": None,
    }
    assert events[-1]["type"] == "done"
    assert runner.decisions == [ApprovalDecision.ACCEPT]
    assert runner.executed == ["pytest"]
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.ACCEPT
    assert [
        (message["role"], message["content"][0]["text"])
        for message in history.json()["messages"]
    ] == [("user", "run the tests"), ("assistant", "All tests passed.")]


def test_cancelling_a_request_ends_the_turn_without_running_the_action() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            decided = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "cancel"},
            )
            streamed = await started
            return decided, streamed, await store.load_approval(request.approval_id)

    decided, streamed, stored = asyncio.run(scenario())

    assert decided.status_code == 200
    assert runner.decisions == [ApprovalDecision.CANCEL]
    assert runner.executed == []
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.CANCEL
    assert _lines(streamed)[-1]["type"] == "done"


def test_a_second_decision_on_the_same_request_is_refused() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            url = _approval_url(thread_id, request.approval_id)
            first = await client.post(url, json={"decision": "accept"})
            second = await client.post(url, json={"decision": "cancel"})
            await started
            return first, second

    first, second = asyncio.run(scenario())

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already decided" in second.json()["error"]
    # The losing decision changed nothing: the command ran once, as approved.
    assert runner.decisions == [ApprovalDecision.ACCEPT]
    assert runner.executed == ["pytest"]


@pytest.mark.parametrize(
    ("decision", "detail"),
    [("maybe", "unknown decision"), ("accept_for_session", "not one of the decisions")],
)
def test_a_decision_the_request_never_offered_is_refused(
    decision: str, detail: str
) -> None:
    """Including one that exists: the provider decides what it can honour."""
    store = InMemoryStateStore()
    runner = ApprovalRunner([WRITE_FILE])
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "write it"})
            )
            request = await _pending(store)
            refused = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": decision},
            )
            unchanged = await store.load_approval(request.approval_id)
            await client.delete(f"/api/threads/{thread_id}/runs/current")
            await started
            return refused, unchanged

    refused, unchanged = asyncio.run(scenario())

    assert refused.status_code == 400
    assert detail in refused.json()["error"]
    # Refusing a decision must not resolve the request it was aimed at.
    assert unchanged is not None
    assert unchanged.is_pending
    assert runner.executed == []


def test_a_request_from_a_finished_run_cannot_be_answered_later() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner([RUN_TESTS, WRITE_FILE])
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            first_run = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "first"})
            )
            stale = await _pending(store)
            await client.post(
                _approval_url(thread_id, stale.approval_id),
                json={"decision": "accept"},
            )
            second = await _pending(store, after=1)
            await client.post(
                _approval_url(thread_id, second.approval_id),
                json={"decision": "accept"},
            )
            await first_run

            # A second turn of the same conversation, and the first turn's
            # request offered again once it is paused.
            runner.requests = (RUN_TESTS,)
            second_run = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "second"})
            )
            current = await _pending(store, after=2)
            refused = await client.post(
                _approval_url(thread_id, stale.approval_id),
                json={"decision": "accept"},
            )
            await client.delete(f"/api/threads/{thread_id}/runs/current")
            await second_run
            return refused, current, await store.list_approvals()

    refused, current, approvals = asyncio.run(scenario())

    assert refused.status_code == 409
    # Two sequential requests in one turn, each its own record, then a third
    # belonging to a run that has nothing to do with them.
    assert [record.command for record in approvals] == ["pytest", None, "pytest"]
    assert approvals[0].agent_run_id == approvals[1].agent_run_id
    assert approvals[2].agent_run_id == current.agent_run_id
    assert approvals[2].agent_run_id != approvals[0].agent_run_id
    assert runner.executed == ["pytest", "Write"]


def test_another_conversations_request_is_not_found_here() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            asking = await _thread(client)
            bystander = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{asking}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            refused = await client.post(
                _approval_url(bystander, request.approval_id),
                json={"decision": "accept"},
            )
            unchanged = await store.load_approval(request.approval_id)
            await client.delete(f"/api/threads/{asking}/runs/current")
            await started
            return refused, unchanged

    refused, unchanged = asyncio.run(scenario())

    assert refused.status_code == 404
    assert unchanged is not None
    assert unchanged.is_pending
    assert runner.executed == []


def test_a_reconnecting_browser_is_told_what_the_run_is_waiting_on() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)
    service = app.state.thread_service

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "run the tests", None)
        request = await _pending(store)

        original = run.stream()
        first = [json.loads((await anext(original)).decode()) for _ in range(2)]
        await original.aclose()  # the browser went away

        # The provider is alive and still waiting, so the pause is still real.
        assert service.active_run(thread.instance_id) is run
        still_pending = await store.load_approval(request.approval_id)

        resumed = run.stream()
        replayed = [json.loads((await anext(resumed)).decode()) for _ in range(2)]
        await resumed.aclose()

        await service.decide_approval(thread.instance_id, request.approval_id, "accept")
        async for _line in run.stream():
            pass
        return first, replayed, still_pending

    first, replayed, still_pending = asyncio.run(scenario())

    assert still_pending is not None
    assert still_pending.is_pending
    assert first[0]["type"] == "approval"
    assert first[0]["approval"]["status"] == "pending"
    assert first[0]["approval"]["command"] == "pytest"
    assert first[1]["type"] == "content"
    # The same pause, told to the connection that arrived after it started.
    assert replayed == first
    assert runner.executed == ["pytest"]


def test_stopping_a_waiting_run_cancels_the_request_and_the_turn() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = _app(store, runner)
    service = app.state.thread_service

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = AgentInstanceId(await _thread(client))
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            run = service.active_run(thread_id)
            assert run is not None
            stopped = await client.delete(f"/api/threads/{thread_id}/runs/current")
            streamed = await started
            return (
                stopped,
                streamed,
                await store.load_approval(request.approval_id),
                await store.agent_run(run.agent_run_id),
                service.approvals.is_waiting(request.approval_id),
                service.active_run(thread_id),
            )

    stopped, streamed, stored, agent_run, waiting, active = asyncio.run(scenario())

    assert stopped.status_code == 204
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.CANCEL
    assert agent_run is not None
    assert agent_run.status is AgentRunStatus.CANCELLED
    # Nothing is left holding the turn open, and nothing is left to answer.
    assert waiting is False
    assert active is None
    assert runner.executed == []
    events = _lines(streamed)
    assert events[-1]["type"] == "done"
    # The client is told the prompt is gone rather than left showing it.
    assert [
        event["approval"]["status"] for event in events if event["type"] == "approval"
    ][-1] == "decided"


def test_a_provider_that_dies_mid_question_leaves_the_request_interrupted() -> None:
    store = InMemoryStateStore()
    runner = DyingApprovalRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            runner.presented.set()  # the CLI goes away mid-question
            streamed = await started
            late = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "accept"},
            )
            return streamed, late, await store.load_approval(request.approval_id)

    streamed, late, stored = asyncio.run(scenario())

    assert stored is not None
    assert stored.status is ApprovalStatus.INTERRUPTED
    assert stored.decision is None
    assert late.status_code == 409
    events = _lines(streamed)
    assert events[-1]["type"] == "error"
    # The prompt stops being a prompt for the client too, not only in the store.
    assert [
        event["approval"]["status"] for event in events if event["type"] == "approval"
    ][-1] == "interrupted"


def test_a_restart_interrupts_the_requests_it_cannot_resume(tmp_path) -> None:
    """The CLI died with the server, so its question can no longer be put."""
    database = tmp_path / "conversations.sqlite3"
    before = SQLiteStateStore(database)
    instance = asyncio.run(before.create_instance(CODER, runner="test"))
    asyncio.run(
        before.record_approval(
            ApprovalRecord(
                approval_id=ApprovalId("apv-before-restart"),
                agent_run_id=AgentRunId("ar-before-restart"),
                instance_id=instance.instance_id,
                runner="test",
                kind=ApprovalKind.COMMAND_EXECUTION,
                requested_at=datetime.now(UTC),
                command="pytest",
                allowed_decisions=tuple(ApprovalDecision),
            )
        )
    )
    before.close()

    after = SQLiteStateStore(database)
    app = _app(after, ApprovalRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/threads")
            refused = await client.post(
                _approval_url(instance.instance_id, "apv-before-restart"),
                json={"decision": "accept"},
            )
            recovered = await after.load_approval(ApprovalId("apv-before-restart"))
            return listed, refused, recovered

    try:
        listed, refused, recovered = asyncio.run(scenario())
    finally:
        after.close()

    assert listed.status_code == 200
    assert recovered is not None
    assert recovered.status is ApprovalStatus.INTERRUPTED
    # The request survives the restart intact; only its answerability changes.
    assert recovered.command == "pytest"
    assert refused.status_code == 409
    assert "interrupted" in refused.json()["error"]


def test_a_runner_that_cannot_pause_runs_exactly_as_before() -> None:
    store = InMemoryStateStore()
    runner = PlainRunner()
    app = _app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "no approvals here"}
            )
            return streamed, await store.list_approvals()

    streamed, approvals = asyncio.run(scenario())

    assert streamed.status_code == 200
    assert _lines(streamed)[-1]["type"] == "done"
    assert approvals == ()
    assert [message.content for message in runner.seen[0]] == ["no approvals here"]
