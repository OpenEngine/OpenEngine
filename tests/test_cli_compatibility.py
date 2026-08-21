"""The approval contract, held against both provider CLIs.

Three decisions -- approve, cancel, allow for this session -- against Codex and
Claude Code. The same three scenarios run twice, over the same code:

* **Against fake CLIs**, which speak the providers' real wire protocols and
  really run the command they are allowed to run. Deterministic, offline, and
  therefore a per-PR blocking test. What it proves is that *our* half of each
  protocol is right: the frames we send, the decisions we translate, the grant
  we replay across a subprocess boundary.
* **Against the pinned real CLIs**, marked `compatibility` and deselected by
  default (see `pyproject.toml`). What it proves is that the providers' half
  has not moved. It needs credentials and somebody else's uptime, which is why
  it runs on a schedule rather than on every pull request.

Both tiers assert on the filesystem rather than on protocol events. "Codex sent
us an approval message" is not the property anybody cares about; "the file the
user refused to allow does not exist" is.

The scenarios are deliberately about *side effects and turns*, not messages:

    approve              the action runs, and the turn carries on
    cancel               the action does not run, and the turn ends
    allow for session    the action runs; the same action in a *later turn* --
                         a new provider subprocess, which remembers nothing --
                         runs without asking again, and a different action in
                         that same turn still asks

The session scenario is the one that has to cross a turn boundary. Inside one
turn, both CLIs already honour their own session grant, so a test that never
started a second turn would pass without any of our persistence existing.
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from engine.adapters.agent_runner.claude_code import (
    READ_ONLY_TOOLS,
    ClaudeCodeAgentRunner,
    approval_request_from_control,
    control_response_for,
)
from engine.adapters.agent_runner.codex import (
    APP_SERVER_DECISIONS,
    INTERACTIVE_APPROVAL_POLICY,
    CodexAgentRunner,
    approval_request_from_app_server,
)
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.apps.web.api import create_app
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalId,
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
)
from engine.ports import AgentRunner
from engine.runtime import AgentSession, Capabilities, normalized_scope

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Do exactly what you are asked, and nothing else.",
        description="Codes.",
    )
}

#: How long a provider may take to ask for consent, and how long a whole turn
#: may take. Two limits rather than one because they fail differently: nothing
#: asking is usually a protocol mismatch, while nothing finishing is usually a
#: model that is still thinking. Both are overridable for a slow runner.
PAUSE_TIMEOUT = float(os.environ.get("ENGINE_COMPAT_PAUSE_TIMEOUT", "180"))
TURN_TIMEOUT = float(os.environ.get("ENGINE_COMPAT_TURN_TIMEOUT", "300"))

#: Deterministic runs need none of that patience: a fake that has not asked
#: within a few seconds is not slow, it is broken.
FAKE_PAUSE_TIMEOUT = 20.0
FAKE_TURN_TIMEOUT = 60.0

#: Where a failing scenario leaves its redacted transcript. Unset locally, so
#: nothing is written; CI sets it and uploads the directory on failure only.
ARTIFACTS = os.environ.get("ENGINE_COMPAT_ARTIFACTS", "")

#: What the live CLIs are told to do. Fixed strings, so a transcript may quote
#: them: everything else a provider sends is redacted, because we cannot know
#: what a model put in it.
APPROVED_COMMAND = "printf 'approved\\n' >> allowed.txt"
SESSION_COMMAND = "printf 'again\\n' >> session.txt"
OTHER_COMMAND = "printf 'other\\n' >> other.txt"
FORBIDDEN_COMMAND = "printf 'forbidden\\n' >> forbidden.txt"
SAFE_TO_QUOTE = frozenset(
    {APPROVED_COMMAND, SESSION_COMMAND, OTHER_COMMAND, FORBIDDEN_COMMAND}
)


# --- what a failure has to say for itself -----------------------------------


class ScenarioFailure(AssertionError):
    """A compatibility failure that names where it happened.

    Which provider, which release, which scenario, and which stage of it: a
    weekly matrix of eighteen cells is only actionable if a red one can be read
    without opening the log.
    """

    def __init__(self, label: str, stage: str, detail: str) -> None:
        super().__init__(f"[{label}] {stage}: {detail}")
        self.label = label
        self.stage = stage


def redacted(value: str | None) -> str | None:
    """Text we are willing to keep, or a fingerprint of text we are not.

    Everything a provider says is somebody's prompt, somebody's source file, or
    somebody's command output until proven otherwise, and none of that belongs
    in a public build artifact. The scenarios' own commands are quotable
    because we wrote them; anything else is reduced to a length and a digest,
    which is still enough to see that two frames carried the same thing.
    """
    if value is None:
        return None
    if value in SAFE_TO_QUOTE:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"<redacted len={len(value)} sha256={digest}>"


@dataclass
class Transcript:
    """What happened, in the shape a stranger could act on.

    Structure only: kinds, decisions, statuses, timings, and event *types*. No
    message text, no command output, no environment. A transcript that could
    leak a prompt would not be uploadable, and one that is not uploadable is
    not a diagnostic.
    """

    label: str
    provider: str
    version: str
    entries: list[dict[str, object]] = field(default_factory=list)

    def note(self, stage: str, **fields: object) -> None:
        self.entries.append({"stage": stage, **fields})

    def note_approval(self, stage: str, record: ApprovalRecord) -> None:
        self.note(
            stage,
            approval_id=str(record.approval_id),
            kind=record.kind.value,
            status=record.status.value,
            decision=record.decision.value if record.decision else None,
            decision_source=(
                record.decision_source.value if record.decision_source else None
            ),
            tool_name=record.tool_name,
            command=redacted(record.command),
            cwd=redacted(record.cwd),
            arguments=redacted(record.arguments),
            normalized_scope=redacted(normalized_scope(record)),
        )

    def write(self) -> None:
        if not ARTIFACTS:
            return
        directory = Path(ARTIFACTS)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.label}.json").write_text(
            json.dumps(
                {
                    "provider": self.provider,
                    "version": self.version,
                    "scenario": self.label,
                    "entries": self.entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


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
        transcript: Transcript,
        *,
        pause_timeout: float,
        turn_timeout: float,
    ) -> None:
        self.client = client
        self.store = store
        self.thread_id = thread_id
        self.transcript = transcript
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
        self.transcript.note(stage, sent=redacted(text), decision=decide.value if decide else None)
        try:
            return await asyncio.wait_for(self._say(text, decide, stage), self._turn_timeout)
        except asyncio.TimeoutError:
            waiting = await self.store.list_approvals(status=ApprovalStatus.PENDING)
            for record in waiting:
                self.transcript.note_approval(f"{stage}: still waiting", record)
            await self.stop()
            raise ScenarioFailure(
                self.transcript.label,
                stage,
                (
                    f"the turn did not finish within {self._turn_timeout:g}s; it is "
                    f"waiting on {[redacted(record.command) for record in waiting]}"
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
                self.transcript.note_approval(f"{stage}: asked", pending)
                answered = await self.client.post(
                    f"/api/threads/{self.thread_id}/runs/current/approvals/"
                    f"{pending.approval_id}",
                    json={"decision": decide.value},
                )
                if answered.status_code != 200:
                    raise ScenarioFailure(
                        self.transcript.label,
                        stage,
                        f"the decision was refused: {answered.status_code} {answered.text}",
                    )
            response = await started
        finally:
            if not started.done():
                started.cancel()
                await asyncio.gather(started, return_exceptions=True)

        raised = tuple((await self.store.list_approvals())[before:])
        for record in raised:
            self.transcript.note_approval(f"{stage}: recorded", record)
        events = tuple(
            json.loads(line) for line in response.text.splitlines() if line.strip()
        )
        self.transcript.note(
            f"{stage}: finished",
            event_types=[event.get("type") for event in events],
            approvals=len(raised),
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
            self.transcript.label,
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
    transcript: Transcript,
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
        transcript,
        pause_timeout=pause_timeout,
        turn_timeout=turn_timeout,
    )


# --- the three scenarios ----------------------------------------------------
#
# One body each, run by both tiers. `ask` turns a command into whatever gets
# that provider to attempt it -- a directive for a fake, an instruction for a
# model.


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


# --- fake CLIs that speak the real protocols --------------------------------
#
# Not mocks of our adapters: real subprocesses, real newline-delimited JSON,
# real approval round trip, and a real `subprocess.run` of the command they are
# allowed to run. What they do not have is a model, so the action comes from a
# directive in the prompt.

DIRECTIVE = "run:"

_DIRECTIVE_READER = '''
def command_from(prompt):
    """The last directive in the transcript is the newest instruction."""
    for line in reversed(prompt.splitlines()):
        if DIRECTIVE in line:
            return line.split(DIRECTIVE, 1)[1].strip()
    raise SystemExit(f"no {DIRECTIVE!r} directive in the prompt")


def execute(command, cwd):
    done = subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True
    )
    return done.returncode, (done.stdout + done.stderr)
'''


def _write_fake(path: Path, body: str) -> str:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\nimport os\nimport subprocess\nimport sys\n\n"
        f"DIRECTIVE = {DIRECTIVE!r}\n\n"
        "def receive():\n    return json.loads(sys.stdin.readline())\n\n"
        "def send(message):\n    print(json.dumps(message), flush=True)\n\n"
        + textwrap.dedent(_DIRECTIVE_READER)
        + "\n"
        + textwrap.dedent(body)
    )
    path.chmod(0o755)
    return str(path)


def fake_codex(directory: Path) -> str:
    """`codex app-server`, as far as one approval and one command go."""
    return _write_fake(
        directory / "codex",
        '''
        initialize = receive()
        send({"id": initialize["id"], "result": {"userAgent": "fake-codex"}})
        assert receive()["method"] == "initialized"

        start = receive()
        assert start["method"] == "thread/start"
        send({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})

        turn = receive()
        assert turn["method"] == "turn/start"
        params = turn["params"]
        cwd = params.get("cwd") or os.getcwd()
        command = command_from(params["input"][0]["text"])
        send({"id": turn["id"], "result": {"turn": {"id": "turn-1"}}})

        send({"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1",
            "item": {"id": "cmd-1", "type": "commandExecution", "command": command,
                     "cwd": cwd, "commandActions": [], "status": "inProgress"}}})
        send({"id": "approval-1", "method": "item/commandExecution/requestApproval",
              "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "cmd-1",
                         "reason": "the command would write outside the sandbox",
                         "command": command, "cwd": cwd, "commandActions": [],
                         "availableDecisions":
                             ["accept", "acceptForSession", "decline", "cancel"]}})
        decision = receive()["result"]["decision"]

        if decision == "cancel":
            # What the real app-server does with a cancelled turn: it ends,
            # and the command never runs.
            send({"method": "turn/completed", "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "items": [], "status": "interrupted"}}})
            raise SystemExit(0)

        exit_code, output = execute(command, cwd)
        send({"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 1,
            "item": {"id": "cmd-1", "type": "commandExecution", "command": command,
                     "cwd": cwd, "commandActions": [], "status": "completed",
                     "exitCode": exit_code, "aggregatedOutput": output}}})
        send({"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 2,
            "item": {"id": "msg-1", "type": "agentMessage",
                     "text": "Ran it, with decision " + decision + "."}}})
        send({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                "last": {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2},
                "total": {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2}}}})
        send({"method": "turn/completed", "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "items": [], "status": "completed"}}})
        ''',
    )


def fake_claude(directory: Path) -> str:
    """`claude --input-format stream-json`, with the control protocol."""
    return _write_fake(
        directory / "claude",
        '''
        initialize = receive()
        send({"type": "control_response", "response": {
            "subtype": "success", "request_id": initialize["request_id"],
            "response": {"commands": []}}})

        user = receive()
        assert user["type"] == "user"
        command = command_from(user["message"]["content"])
        cwd = os.getcwd()

        send({"type": "system", "subtype": "init", "session_id": "session-1"})
        send({"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "toolu_1", "name": "Bash",
            "input": {"command": command}}]}})
        send({"type": "control_request", "request_id": "permission-1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash",
            "input": {"command": command}, "tool_use_id": "toolu_1",
            "title": "Claude wants to run a command",
            "permission_suggestions": [{"type": "addRules", "rules": [{
                "toolName": "Bash", "ruleContent": command}],
                "behavior": "allow", "destination": "localSettings"}]}})
        answer = receive()["response"]["response"]

        if answer["behavior"] == "deny":
            # deny + interrupt ends the turn, and the command never runs.
            send({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "result": answer.get("message", "denied"),
                  "usage": {}})
            raise SystemExit(0)

        exit_code, output = execute(command, cwd)
        send({"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_1",
            "content": output or "(no output)", "is_error": exit_code != 0}]}})
        send({"type": "assistant", "message": {"content": [{
            "type": "text", "text": "Ran it."}]}})
        send({"type": "result", "subtype": "success", "is_error": False,
              "result": "Ran it.", "usage": {}})
        ''',
    )


FAKES = {
    "codex": (
        fake_codex,
        lambda binary, workspace: CodexAgentRunner(
            binary_path=binary, working_directory=str(workspace)
        ),
    ),
    "claude": (
        fake_claude,
        lambda binary, workspace: ClaudeCodeAgentRunner(
            binary_path=binary, working_directory=str(workspace)
        ),
    ),
}


@pytest.mark.parametrize("provider", sorted(FAKES))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_approval_contract_holds_over_each_protocol(
    provider: str, scenario: str, tmp_path
) -> None:
    """Six cells with no model in them: our half of both protocols, end to end."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    build_fake, build_runner = FAKES[provider]
    binary = build_fake(tmp_path)
    transcript = Transcript(f"fake-{provider}-{scenario}", provider, "fake")

    async def run() -> None:
        client, chat = await open_chat(
            build_runner(binary, workspace),
            transcript,
            runner_name=provider,
            pause_timeout=FAKE_PAUSE_TIMEOUT,
            turn_timeout=FAKE_TURN_TIMEOUT,
        )
        try:
            await SCENARIOS[scenario](chat, workspace, lambda command: f"{DIRECTIVE} {command}")
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    finally:
        transcript.write()


# --- protocol fixtures for what a grant is scoped by ------------------------
#
# The scenarios above only exercise command execution, because that is what a
# fake can meaningfully carry out. File changes are the other kind of request
# both providers raise, and they normalize differently -- by path rather than by
# command line -- so they are pinned here from captured protocol frames.


CODEX_FILE_CHANGE = {
    "id": 42,
    "method": "item/fileChange/requestApproval",
    "params": {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "change-1",
        "reason": "the edit is outside the writable roots",
        "cwd": "/workspace",
        "changes": {"/workspace/src/app.py": {"kind": "update"}},
        "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
    },
}

CLAUDE_FILE_CHANGE = {
    "type": "control_request",
    "request_id": "permission-2",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Write",
        "tool_use_id": "toolu_2",
        "input": {"file_path": "/workspace/src/app.py", "content": "print(1)\n"},
        "title": "Claude wants to write src/app.py",
        "permission_suggestions": [
            {
                "type": "addRules",
                "rules": [{"toolName": "Write", "ruleContent": "/workspace/src/app.py"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ],
    },
}

CODEX_COMMAND = {
    "id": 43,
    "method": "item/commandExecution/requestApproval",
    "params": {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "cmd-1",
        "command": "pytest -q",
        "cwd": "/workspace",
        "commandActions": [{"type": "run", "argv": ["pytest", "-q"]}],
        "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
    },
}

CLAUDE_COMMAND = {
    "type": "control_request",
    "request_id": "permission-3",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "tool_use_id": "toolu_3",
        "input": {"command": "pytest -q", "description": "run the tests"},
        "permission_suggestions": [
            {
                "type": "addRules",
                "rules": [{"toolName": "Bash", "ruleContent": "pytest -q"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ],
    },
}


def _scope_of(request, *, cwd: str | None = None) -> str | None:
    """The scope a broker would store for one normalized provider request."""
    return normalized_scope(
        ApprovalRecord(
            approval_id=ApprovalId("apv-1"),
            agent_run_id=AgentRunId("ar-1"),
            instance_id=AgentInstanceId("agi-1"),
            runner="test",
            kind=request.kind,
            requested_at=datetime.now(UTC),
            command=request.command,
            cwd=request.cwd or cwd,
            tool_name=request.tool_name,
            arguments=request.arguments,
            allowed_decisions=request.allowed_decisions,
        )
    )


def test_both_providers_normalize_a_command_execution_the_same_way() -> None:
    codex = approval_request_from_app_server(CODEX_COMMAND)
    claude = approval_request_from_control(CLAUDE_COMMAND)

    assert codex is not None and claude is not None
    assert codex.kind is ApprovalKind.COMMAND_EXECUTION
    assert claude.kind is ApprovalKind.COMMAND_EXECUTION
    assert codex.command == claude.command == "pytest -q"
    # Codex states the directory; Claude does not, so its request is scoped by
    # the worktree the turn is running in instead. Told the same directory,
    # both reduce to the same one thing.
    assert _scope_of(codex) == "command_execution|/workspace|pytest -q"
    assert _scope_of(claude, cwd="/workspace") == _scope_of(codex)
    assert ApprovalDecision.ACCEPT_FOR_SESSION in codex.allowed_decisions
    assert ApprovalDecision.ACCEPT_FOR_SESSION in claude.allowed_decisions


def test_both_providers_normalize_a_file_change_to_the_file() -> None:
    codex = approval_request_from_app_server(CODEX_FILE_CHANGE)
    claude = approval_request_from_control(CLAUDE_FILE_CHANGE)

    assert codex is not None and claude is not None
    assert codex.kind is ApprovalKind.FILE_CHANGE
    assert claude.kind is ApprovalKind.FILE_CHANGE
    # Codex describes a map of path to edit, Claude a tool input with a path.
    # A grant is scoped to the file either way -- never to the content, which
    # differs on every write and would make the grant unmatchable.
    assert _scope_of(codex) == "file_change|/workspace/src/app.py"
    assert _scope_of(claude) == _scope_of(codex)


def test_a_codex_request_we_do_not_recognise_is_not_an_approval() -> None:
    """Fail closed: an unknown server request is refused by the transport
    rather than answered with a guessed shape."""
    assert (
        approval_request_from_app_server(
            {**CODEX_COMMAND, "method": "item/network/requestApproval"}
        )
        is None
    )
    assert approval_request_from_control(
        {**CLAUDE_COMMAND, "request": {"subtype": "mcp_elicitation"}}
    ) is None


def test_allowing_for_the_session_is_never_a_provider_wide_bypass() -> None:
    """The failure mode worth a test of its own.

    "Allow this for the session" is one decision about one action. Every cheap
    way to implement it -- `approval_policy = never`, a sandbox set to
    full access, `--dangerously-skip-permissions`, a rule written into the
    user's settings file -- turns it into a standing yes to things they were
    never shown. Each is checked here because each would still pass every
    behavioural test above.
    """
    assert INTERACTIVE_APPROVAL_POLICY == "on-request"
    assert APP_SERVER_DECISIONS[ApprovalDecision.ACCEPT_FOR_SESSION] == "acceptForSession"
    assert "never" not in APP_SERVER_DECISIONS.values()

    granted = control_response_for(CLAUDE_COMMAND, ApprovalDecision.ACCEPT_FOR_SESSION)
    permissions = granted["response"]["response"]["updatedPermissions"]
    assert granted["response"]["response"]["behavior"] == "allow"
    # The provider's own session, which dies with the process -- not the user's
    # settings file, which does not.
    assert [rule["destination"] for rule in permissions] == ["session"]
    assert [rule["rules"] for rule in permissions] == [
        [{"toolName": "Bash", "ruleContent": "pytest -q"}]
    ]

    claude_argv = ClaudeCodeAgentRunner().interactive_command_line(PROFILES[CODER])
    codex_argv = CodexAgentRunner(sandbox="workspace-write").command_line(
        PROFILES[CODER]
    )
    assert "--dangerously-skip-permissions" not in claude_argv
    assert "--permission-mode" not in claude_argv
    assert "danger-full-access" not in codex_argv


# --- the version manifest ---------------------------------------------------


MANIFEST = Path(__file__).resolve().parents[1] / ".github" / "cli-versions.json"


def test_the_pinned_matrix_is_a_manifest_rather_than_a_moving_target() -> None:
    """A release joins or leaves the matrix through this file, reviewably."""
    manifest = json.loads(MANIFEST.read_text())
    wanted = manifest["policy"]["versions"]

    assert wanted == 3, "the ticket's matrix is the latest three of each CLI"
    for provider in ("codex", "claude"):
        pinned = manifest[provider]
        assert isinstance(pinned, list)
        assert all(isinstance(version, str) and version for version in pinned)
        assert len(set(pinned)) == len(pinned), f"{provider} pins a version twice"
        # Exactly the policy, checked here rather than only in the scheduled
        # workflow: a change that drops a release without replacing it is how
        # the matrix quietly shrinks, and that belongs in the review of the
        # commit that does it.
        assert len(pinned) == wanted, f"{provider} pins {len(pinned)} of {wanted}"


def test_the_compatibility_workflow_installs_what_the_manifest_pins() -> None:
    """Otherwise the manifest would be documentation rather than a control."""
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "cli-compatibility.yml"
    ).read_text()

    assert ".github/cli-versions.json" in workflow
    assert "${{ matrix.package }}@${{ matrix.version }}" in workflow
    # A floating install would make a red cell unreproducible and a green one
    # meaningless.
    assert "@latest" not in workflow
    assert "pull_request" not in workflow, "live provider runs do not gate PRs"


# --- the live matrix --------------------------------------------------------
#
# Deselected by default. `-m compatibility` runs them, and CI does that only in
# the scheduled workflow, one pinned CLI at a time.

LIVE = {
    # Read-only rather than the chat default of workspace-write, because the
    # sandbox is what decides whether there is an approval to test at all: with
    # a writable workspace Codex would simply create the file, and the scenario
    # would be waiting for a question nobody was ever going to ask. Read-only
    # plus `on-request` makes every write an escalation, which is the request
    # this matrix exists to answer three different ways.
    "codex": (
        "codex",
        lambda binary, workspace: CodexAgentRunner(
            binary_path=binary,
            sandbox="read-only",
            working_directory=str(workspace),
        ),
    ),
    "claude": (
        "claude",
        lambda binary, workspace: ClaudeCodeAgentRunner(
            binary_path=binary,
            allowed_tools=READ_ONLY_TOOLS,
            working_directory=str(workspace),
        ),
    ),
}


def installed_version(binary: str) -> str | None:
    """What the CLI calls itself, or None if it is not usable here."""
    if shutil.which(binary) is None:
        return None
    try:
        reported = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return reported.stdout.strip() if reported.returncode == 0 else None


def live_instruction(command: str) -> str:
    """Ask a model for one specific command, and nothing else.

    Specific because the session scenario compares the *second* request against
    the first: a model that paraphrases its own shell command is asking for a
    different thing, and being asked again is the correct response to that.
    """
    return (
        "Run exactly this shell command in the current working directory, "
        f"verbatim and as a single command:\n\n{command}\n\n"
        "Do not run any other command, do not read or write any other file, "
        "and do not use a file-editing tool. Reply with only the word DONE."
    )


@pytest.mark.compatibility
@pytest.mark.parametrize("provider", sorted(LIVE))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_approval_contract_holds_against_the_installed_cli(
    provider: str, scenario: str, tmp_path
) -> None:
    """One cell of the matrix: this provider, this release, this decision."""
    only = os.environ.get("ENGINE_COMPAT_PROVIDER")
    if only and only != provider:
        pytest.skip(f"this job runs {only}, not {provider}")

    binary, build_runner = LIVE[provider]
    version = installed_version(binary)
    if version is None:
        pytest.skip(f"{binary} is not installed or not runnable on PATH")

    # A fresh directory per cell: a scenario that asserts a file was *not*
    # created cannot share a workspace with one that creates files.
    root = os.environ.get("ENGINE_COMPAT_WORKSPACE")
    workspace = Path(root) / f"{provider}-{scenario}" if root else tmp_path / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    transcript = Transcript(
        f"{provider}-{os.environ.get('ENGINE_COMPAT_VERSION', version)}-{scenario}",
        provider,
        version,
    )
    transcript.note("installed", version=version, binary=binary)

    async def run() -> None:
        client, chat = await open_chat(
            build_runner(binary, workspace),
            transcript,
            runner_name=provider,
            pause_timeout=PAUSE_TIMEOUT,
            turn_timeout=TURN_TIMEOUT,
        )
        try:
            await SCENARIOS[scenario](chat, workspace, live_instruction)
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    except BaseException as failure:
        # Our own failures say where they happened and are already redacted; a
        # provider's carries a tail of its stderr, which is somebody's output.
        transcript.note(
            "failed",
            error=type(failure).__name__,
            detail=(
                str(failure)
                if isinstance(failure, ScenarioFailure)
                else redacted(str(failure))
            ),
        )
        raise
    finally:
        transcript.write()
