"""The approval contract: approve, cancel, and allow for this session.

Shared by the tests that drive an agent runner through the web app, so the
three scenarios are one body each however the runner reaches its agent. They
assert on the filesystem rather than on protocol events: "the agent sent us an
approval message" is not the property anybody cares about; "the file the user
refused to allow does not exist" is.

    approve              the action runs, and the turn carries on
    cancel               the action does not run, and the turn ends
    allow for session    the action runs; the same action in a *later turn* --
                         a new agent process, which remembers nothing -- runs
                         without asking again, and a different action in that
                         same turn still asks

The session scenario is the one that has to cross a turn boundary. Inside one
turn, an agent already honours its own session grant, so a test that never
started a second turn would pass without any of our persistence existing.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.apps.web.api import create_app
from engine.domain import (
    AgentId,
    AgentProfile,
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalRecord,
    ApprovalStatus,
)
from engine.ports import AgentRunner
from engine.runtime import AgentSession, Capabilities

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Do exactly what you are asked, and nothing else.",
        description="Codes.",
    )
}

#: How long the agent may take to ask for consent, and how long a whole turn
#: may take. A fake that has not asked within a few seconds is not slow, it is
#: broken.
FAKE_PAUSE_TIMEOUT = 20.0
FAKE_TURN_TIMEOUT = 60.0

#: What the agent is told to do.
APPROVED_COMMAND = "printf 'approved\\n' >> allowed.txt"
SESSION_COMMAND = "printf 'again\\n' >> session.txt"
OTHER_COMMAND = "printf 'other\\n' >> other.txt"
FORBIDDEN_COMMAND = "printf 'forbidden\\n' >> forbidden.txt"


# --- what a failure has to say for itself -----------------------------------


class ScenarioFailure(AssertionError):
    """A compatibility failure that names where it happened.

    Which provider, which scenario, and which stage of it, so a failure can be
    read without opening the log.
    """

    def __init__(self, label: str, stage: str, detail: str) -> None:
        super().__init__(f"[{label}] {stage}: {detail}")
        self.label = label
        self.stage = stage


# --- driving one conversation -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn, and what it did on the way through."""

    events: tuple[dict[str, object], ...]
    approvals: tuple[ApprovalRecord, ...]
    """Only the requests this turn raised, in the order it raised them."""

    @property
    def paused_for(self) -> tuple[ApprovalRecord, ...]:
        """The requests that were actually put to a person.

        Everything answered without one is excluded -- a grant, or the
        configured policy -- and nothing else is: a request decided by the user
        was a card they were shown, and one left undecided was a card nobody got
        to.
        """
        answered_for_them = {
            ApprovalDecisionSource.SESSION_GRANT,
            ApprovalDecisionSource.POLICY,
        }
        return tuple(
            record
            for record in self.approvals
            if record.decision_source not in answered_for_them
        )


class Chat:
    """A conversation driven the way the browser drives one.

    Through the HTTP surface rather than the broker, because the thing under
    test is the whole path: a decision arrives on a different request from the
    one that showed the pause, and a turn that never presented the pause would
    still pass a broker-level test.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        store: InMemoryStateStore,
        thread_id: str,
        label: str,
        *,
        pause_timeout: float,
        turn_timeout: float,
    ) -> None:
        self.client = client
        self.store = store
        self.thread_id = thread_id
        self.label = label
        self._pause_timeout = pause_timeout
        self._turn_timeout = turn_timeout

    async def say(
        self, text: str, *, decide: ApprovalDecision | None = None, stage: str
    ) -> Turn:
        """One turn. `decide` answers the first request it raises, if any.

        With no decision to give, a turn that pauses can only end in the
        timeout -- which is the correct outcome for "this should not have
        asked", and is reported as exactly that.
        """
        try:
            return await asyncio.wait_for(self._say(text, decide, stage), self._turn_timeout)
        except asyncio.TimeoutError:
            waiting = await self.store.list_approvals(status=ApprovalStatus.PENDING)
            await self.stop()
            raise ScenarioFailure(
                self.label,
                stage,
                (
                    f"the turn did not finish within {self._turn_timeout:g}s; it is "
                    f"waiting on {[record.command for record in waiting]}"
                    if waiting
                    else f"the turn did not finish within {self._turn_timeout:g}s"
                ),
            ) from None

    async def _say(
        self, text: str, decide: ApprovalDecision | None, stage: str
    ) -> Turn:
        before = len(await self.store.list_approvals())
        started = asyncio.create_task(
            self.client.post(
                f"/api/threads/{self.thread_id}/runs", json={"text": text}
            )
        )
        try:
            if decide is not None:
                pending = await self._await_pending(before, stage)
                answered = await self.client.post(
                    f"/api/threads/{self.thread_id}/runs/current/approvals/"
                    f"{pending.approval_id}",
                    json={"decision": decide.value},
                )
                if answered.status_code != 200:
                    raise ScenarioFailure(
                        self.label,
                        stage,
                        f"the decision was refused: {answered.status_code} {answered.text}",
                    )
            response = await started
        finally:
            if not started.done():
                started.cancel()
                await asyncio.gather(started, return_exceptions=True)

        raised = tuple((await self.store.list_approvals())[before:])
        events = tuple(
            json.loads(line) for line in response.text.splitlines() if line.strip()
        )
        return Turn(events=events, approvals=raised)

    async def _await_pending(self, after: int, stage: str) -> ApprovalRecord:
        """Wait for the provider to ask, and say something useful if it never does."""
        deadline = asyncio.get_running_loop().time() + self._pause_timeout
        while asyncio.get_running_loop().time() < deadline:
            approvals = await self.store.list_approvals()
            if len(approvals) > after and approvals[-1].is_pending:
                return approvals[-1]
            await asyncio.sleep(0.02)
        raise ScenarioFailure(
            self.label,
            stage,
            f"the provider did not ask for approval within {self._pause_timeout:g}s",
        )

    async def stop(self) -> None:
        """Best effort, so a failed scenario does not leave a CLI running."""
        try:
            await self.client.delete(f"/api/threads/{self.thread_id}/runs/current")
        except Exception:  # pragma: no cover - teardown of an already-broken run
            pass

    async def grants(self):
        return await self.store.list_session_grants()


async def open_chat(
    runner: AgentRunner,
    label: str,
    *,
    runner_name: str,
    pause_timeout: float,
    turn_timeout: float,
):
    """Compose the app the way `apps/web` does, over one runner."""
    store = InMemoryStateStore()
    unused = object()
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=unused,
            state_store=store,
        ),
        profiles=PROFILES,
        runners={runner_name: runner},
    )
    app = create_app(session, {runner_name: runner})
    # No timeout: a live turn takes as long as the model takes, and the
    # scenario's own deadline is the one that should end it.
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=None
    )
    created = await client.post(
        "/api/threads", json={"agentId": "coder", "runner": runner_name}
    )
    return client, Chat(
        client,
        store,
        created.json()["id"],
        label,
        pause_timeout=pause_timeout,
        turn_timeout=turn_timeout,
    )


# --- the three scenarios ----------------------------------------------------
#
# One body each. `ask` turns a command into whatever gets the agent to attempt
# it -- for a fake, a `run:` directive.


async def approve_scenario(chat: Chat, workspace: Path, ask: Callable[[str], str]) -> None:
    """`ACCEPT`: the requested action executes and the turn continues."""
    created = workspace / "allowed.txt"
    turn = await chat.say(
        ask(APPROVED_COMMAND), decide=ApprovalDecision.ACCEPT, stage="approve"
    )

    assert turn.approvals, "the provider never asked for approval"
    decided = turn.approvals[0]
    assert decided.decision is ApprovalDecision.ACCEPT
    assert decided.decision_source is ApprovalDecisionSource.USER
    assert decided.status is ApprovalStatus.DECIDED
    # The file, not the event: a provider that acknowledges an approval and
    # then does nothing has failed the thing the approval was for.
    assert created.is_file(), f"{created} was approved but never created"
    assert turn.events[-1]["type"] == "done", "the turn did not carry on to an answer"


async def cancel_scenario(chat: Chat, workspace: Path, ask: Callable[[str], str]) -> None:
    """`CANCEL`: the action does not execute and the turn terminates."""
    forbidden = workspace / "forbidden.txt"
    turn = await chat.say(
        ask(FORBIDDEN_COMMAND), decide=ApprovalDecision.CANCEL, stage="cancel"
    )

    assert turn.approvals, "the provider never asked for approval"
    decided = turn.approvals[0]
    assert decided.decision is ApprovalDecision.CANCEL
    assert decided.status is ApprovalStatus.DECIDED
    # The whole point of a denial, checked the only way that means anything.
    assert not forbidden.exists(), f"{forbidden} was created despite being cancelled"
    assert turn.events[-1]["type"] in {"done", "error"}, "the turn never ended"


async def session_scenario(chat: Chat, workspace: Path, ask: Callable[[str], str]) -> None:
    """`ACCEPT_FOR_SESSION`: allowed once, then reused across a turn boundary."""
    log = workspace / "session.txt"

    first = await chat.say(
        ask(SESSION_COMMAND),
        decide=ApprovalDecision.ACCEPT_FOR_SESSION,
        stage="session: first turn",
    )
    assert first.approvals, "the provider never asked for approval"
    assert first.approvals[0].decision is ApprovalDecision.ACCEPT_FOR_SESSION
    assert log.is_file(), f"{log} was approved but never created"
    first_lines = log.read_text().count("again")

    grants = await chat.grants()
    assert len(grants) == 1, f"expected one session grant, got {grants}"
    assert grants[0].is_active
    assert grants[0].created_from_approval_id == first.approvals[0].approval_id

    # A second turn: a provider subprocess that has never heard of the first.
    # Nobody answers anything here -- if it pauses, the turn timeout fires and
    # says so, which is the failure this scenario exists to catch.
    second = await chat.say(ask(SESSION_COMMAND), stage="session: second turn")
    assert second.approvals, "the second turn never raised the request at all"
    replayed = second.approvals[-1]
    assert replayed.decision is ApprovalDecision.ACCEPT_FOR_SESSION
    assert replayed.decision_source is ApprovalDecisionSource.SESSION_GRANT
    assert replayed.status is ApprovalStatus.DECIDED
    assert not second.paused_for, "the second turn asked again despite the grant"
    assert log.read_text().count("again") > first_lines, "the reused approval did nothing"

    # And the grant covers what it was given for and nothing else.
    other = await chat.say(
        ask(OTHER_COMMAND), decide=ApprovalDecision.CANCEL, stage="session: another action"
    )
    assert other.paused_for, "a different action was allowed without asking"
    assert not (workspace / "other.txt").exists()
    assert len(await chat.grants()) == 1, "cancelling minted a grant"


SCENARIOS: dict[str, Callable] = {
    "approve": approve_scenario,
    "cancel": cancel_scenario,
    "session": session_scenario,
}
