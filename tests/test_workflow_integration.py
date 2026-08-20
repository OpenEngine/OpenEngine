"""Integration coverage for a complete implementation-review workflow."""

import asyncio
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette

import engine.runtime.dispatcher as dispatcher_module
import github_fakes
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workspace_provider.git_worktree import (
    GitWorktreeWorkspaceProvider,
)
from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_review_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.domain import (
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    HumanReviewCompleted,
    Message,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    StepCompleted,
    StepSpec,
    ToolSpec,
    WorkspaceId,
    WorkspaceProvisioned,
)
from engine.ports import AgentTurn, McpServerConfig
from engine.runtime import AgentSession, Capabilities
from engine.runtime.step_results import (
    INVALID_COMPLETION_ERROR,
    step_completed_from_arguments,
)
from engine.runtime.terminal_mcp import (
    TerminalDelivery,
    TerminalEvent,
    TerminalResultRegistry,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR
from provider_fakes import (
    SCRIPT_ENVIRONMENT_VARIABLE,
    fake_claude,
    fake_codex,
)


_IDENTITY = ("-c", "user.name=Engine Tests", "-c", "user.email=engine@example.test")


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repository / "README.md").write_text("integration fixture\n")
    _git(repository, "add", "README.md")
    _git(repository, *_IDENTITY, "commit", "-m", "initial")
    subprocess.run(
        ["git", "clone", "--bare", str(repository), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repository, "remote", "add", "origin", str(remote))
    return repository


class MockTerminalMcpBroker:
    """Replace only the MCP transport while retaining terminal event binding."""

    sessions: dict[str, "MockTerminalMcpBroker"] = {}

    def __init__(
        self,
        *,
        run_id: RunId,
        agent_run_id: AgentRunId,
        step: StepSpec,
        registry: TerminalResultRegistry,
        deliver: TerminalDelivery | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_run_id = agent_run_id
        self.step = step
        self.registry = registry
        self.deliver = deliver
        self.token = uuid4().hex
        self._result: asyncio.Future[TerminalEvent] | None = None

    async def __aenter__(self) -> "MockTerminalMcpBroker":
        self._result = asyncio.get_running_loop().create_future()
        self.sessions[self.token] = self
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.sessions.pop(self.token, None)
        if self._result is not None and not self._result.done():
            self._result.cancel()

    @property
    def config(self) -> McpServerConfig:
        return McpServerConfig("workflow", "mock-mcp", (self.token,))

    async def result(self) -> TerminalEvent:
        assert self._result is not None
        return await asyncio.shield(self._result)

    async def complete(
        self, request_id: str, arguments: dict[str, object]
    ) -> None:
        event = step_completed_from_arguments(
            run_id=self.run_id,
            step=self.step,
            agent_run_id=self.agent_run_id,
            arguments=arguments,
            mcp_request_id=request_id,
        )
        await self.registry.accept(self.agent_run_id, event, self.deliver)
        assert self._result is not None
        self._result.set_result(event)


class CompletingRunner:
    """A provider stand-in whose only behavior is its mocked MCP call."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.calls: list[tuple[AgentRunId, WorkspaceId | None]] = []
        self.naming_calls: list[tuple[Message, ...]] = []
        self.cancelled = asyncio.Event()

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        self.naming_calls.append(tuple(messages))
        return AgentTurn(Message.assistant('"Exercise complete workflow"'))

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        self.calls.append((agent_run_id, workspace_id))
        broker = MockTerminalMcpBroker.sessions[mcp_server.args[0]]
        await broker.complete(f"{broker.step.step_id}-call", self.arguments)
        await self.cancelled.wait()
        return AgentTurn(Message.assistant("Terminal result accepted."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        self.cancelled.set()


async def _await_phase(
    client: httpx.AsyncClient, run_id: RunId, phase: str, attempts: int = 200
) -> httpx.Response:
    for _ in range(attempts):
        response = await client.get(f"/api/runs/{run_id}")
        if response.json()["phase"] == phase:
            return response
        await asyncio.sleep(0.01)
    return response


def test_implementation_review_workflow_completes_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_module, "TerminalMcpBroker", MockTerminalMcpBroker
    )
    database = tmp_path / "workflow.sqlite3"
    repository = _repository(tmp_path)
    store = SQLiteStateStore(database)
    workspaces = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    implementer = CompletingRunner(
        {
            "outcome": "success",
            "summary": "Implemented the requested change.",
            "outputs": {"pr_url": "https://github.com/acme/api/pull/42"},
        }
    )
    reviewer = CompletingRunner(
        {
            "outcome": "success",
            "summary": "The implementation satisfies the task.",
            "outputs": {"findings": "No blocking findings."},
        }
    )
    unused = object()
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=implementer,
            communications=unused,
            workspace_provider=workspaces,
            state_store=store,
        ),
        profiles={},
        runners={"test": reviewer},
    )
    app = create_app(
        session,
        {"test": reviewer},
        workflow_runners={"test": implementer},
        review_runners={"test": reviewer},
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-v1",
                        "prompt": "Exercise the complete workflow.",
                        "repository": str(repository),
                        "runner": "test",
                    },
                )
                run_id = RunId(created.json()["runId"])
                awaiting = await _await_phase(
                    client, run_id, "awaiting_human_review"
                )
                completed = await client.post(
                    f"/api/runs/{run_id}/human-review",
                    json={
                        "approved": True,
                        "summary": "Approved by the integration test.",
                    },
                )
                agent_run_ids = [
                    AgentRunId(step["agentRunId"])
                    for step in completed.json()["steps"]
                    if step["agentRunId"] is not None
                ]
                for _ in range(200):
                    agent_runs = [
                        await store.agent_run(agent_run_id)
                        for agent_run_id in agent_run_ids
                    ]
                    if all(
                        run and run.status is AgentRunStatus.SUCCEEDED
                        for run in agent_runs
                    ):
                        break
                    await asyncio.sleep(0.01)
        state = await store.load(run_id)
        assert state is not None
        assert state.workspace_id is not None
        history = await store.history(run_id)
        instances = await store.list_instances(workflow_run_id=run_id)
        conversations = [
            await store.load_conversation(instance.instance_id)
            for instance in instances
        ]
        return awaiting, completed, state, history, instances, conversations, agent_runs

    awaiting, completed, state, history, instances, conversations, agent_runs = (
        asyncio.run(scenario())
    )
    store.close()
    reopened_store = SQLiteStateStore(database)
    reopened_state = asyncio.run(reopened_store.load(state.run_id))
    reopened_store.close()

    assert awaiting.json()["phase"] == "awaiting_human_review"
    assert completed.status_code == 200
    assert completed.json()["phase"] == "succeeded"
    assert completed.json()["terminalOutcome"] == "approved"
    assert completed.json()["pendingHumanReview"] is None
    assert completed.json()["humanDecision"] == {
        "stepId": "human-review",
        "approved": True,
        "outcome": "approved",
        "summary": "Approved by the integration test.",
    }
    assert state.phase is RunPhase.SUCCEEDED
    assert state.name == "Exercise complete workflow"
    assert completed.json()["name"] == state.name
    assert reopened_state == state
    assert [type(event) for event in history] == [
        RunRequested,
        WorkspaceProvisioned,
        RunNamed,
        StepCompleted,
        StepCompleted,
        HumanReviewCompleted,
    ]
    assert [result.mcp_request_id for result in state.step_results] == [
        "implementation-call",
        "review-call",
    ]
    assert len(instances) == 2
    assert all(
        conversation and conversation.messages for conversation in conversations
    )
    assert all(
        run and run.status is AgentRunStatus.SUCCEEDED for run in agent_runs
    ), agent_runs
    assert implementer.calls[0][1] == reviewer.calls[0][1] == state.workspace_id
    assert implementer.naming_calls == [
        (
            Message.user("Exercise the complete workflow."),
            Message.user(
                "Name this workflow based on the task above. Do not perform the task "
                "or use tools. Reply with only a concise name of at most eight words, "
                "with no quotes or ending punctuation."
            ),
        )
    ]
    assert reviewer.naming_calls == []
    assert asyncio.run(workspaces.state(state.workspace_id)).attached


# --- the same workflow, driven by scripted CLIs over the real MCP bridge -----
#
# Everything above replaces the transport to isolate the reducer. These replace
# only the model: real provider subprocesses, the real terminal MCP server, a
# real worktree, and a `gh` that records instead of commenting. This is the tier
# a protocol mistake should be caught in -- it is seconds rather than a browser
# run, and the browser tier then only has to prove the interface.

TASK = "Add a greeting file to the repository."
GREETING_COMMAND = "echo hello > greeting.txt"
PULL_REQUEST = "https://github.com/acme/api/pull/7"
FINDING = "greeting.txt is not covered by a test."

#: The reviewer's prompt quotes the task the implementation was given, so the
#: scenario only a reviewer can match is listed first: the first match wins.
SCRIPTED_RUN = {
    "title": "Adding a greeting",
    "scenarios": [
        {
            "when": "Inspect the workspace",
            "steps": [
                {"type": "say", "text": "Read the change; one thing to note."},
                {
                    "type": "tool",
                    "name": "add_comment",
                    "arguments": {
                        "pr_url": PULL_REQUEST,
                        "comment": FINDING,
                        "file": "greeting.txt",
                        "line": 1,
                    },
                },
                {
                    "type": "tool",
                    "name": "complete_step",
                    "arguments": {
                        "outcome": "success",
                        "summary": "Reviewed the greeting.",
                        "outputs": {"findings": FINDING},
                    },
                },
                {"type": "say", "text": "Left the finding on the pull request."},
            ],
        },
        {
            "when": "greeting",
            "steps": [
                {"type": "say", "text": "Writing the greeting."},
                {"type": "run", "command": GREETING_COMMAND, "approval": False},
                {
                    "type": "tool",
                    "name": "complete_step",
                    "arguments": {
                        "outcome": "success",
                        "summary": "Added the greeting.",
                        "outputs": {"pr_url": PULL_REQUEST},
                    },
                },
                # A turn ends in the agent's answer, and a turn that finishes
                # before the runtime cancels it is only assembled in the order
                # it was streamed when its last item is a message.
                {"type": "say", "text": "Wrote greeting.txt."},
            ],
        },
    ],
}


def _compose(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: object,
) -> tuple[Starlette, Capabilities, Path]:
    """The web application, composed as it is in production over fake CLIs.

    The composition root rather than a hand-wired capability set: which runner
    reviews, which one may write, and which binaries they are is exactly what
    this is checking, and a test that wired those itself would be checking its
    own wiring.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir()
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    monkeypatch.setenv(SCRIPT_ENVIRONMENT_VARIABLE, str(script_path))
    gh_log = tmp_path / "gh.jsonl"

    settings = Settings(
        codex_binary=fake_codex(binaries),
        claude_binary=fake_claude(binaries),
        github_binary=github_fakes.install(binaries, gh_log),
        codex_working_directory=str(repository),
        claude_working_directory=str(repository),
        workspace_root=str(tmp_path / "workspaces"),
        sqlite_path=str(tmp_path / "conversations.sqlite3"),
    )
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    app = create_app(
        build_session(capabilities, runners, str(repository)),
        runners,
        workflow_runners=build_workflow_runners(settings),
        review_runners=build_review_runners(settings),
    )
    return app, capabilities, gh_log


async def _drive(
    app: Starlette, repository: Path, prompt: str, runner: str, phase: str
) -> tuple[dict, list[dict]]:
    """Create a run on `runner` and wait for it to reach `phase`."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": prompt,
                    "repository": str(repository),
                    "runner": runner,
                },
            )
            run_id = RunId(created.json()["runId"])
            # Subprocesses rather than coroutines: a CLI per turn, an MCP
            # server per tool call, and a `gh` are spawned on the way here.
            reached = await _await_phase(client, run_id, phase, attempts=6_000)
            # A workflow step's conversation is reached through its run rather
            # than from the chat list, which holds only chats.
            threads = [
                (await client.get(f"/api/threads/{step['agentInstanceId']}")).json()
                for step in reached.json()["steps"]
                if step["agentInstanceId"]
            ]
    return reached.json(), threads


async def _said(store: SQLiteStateStore, run_id: RunId) -> list[str]:
    """Everything a run's conversations durably hold, from the store itself.

    Read here rather than through the API because the API hides an in-flight
    assistant transcript for the run stream to replay, and a failed run is only
    just no longer in flight.
    """
    said: list[str] = []
    for instance in await store.list_instances(workflow_run_id=run_id):
        conversation = await store.load_conversation(instance.instance_id)
        if conversation is None:
            continue
        said.extend(message.content for message in conversation.messages)
    return said


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_scripted_cli_drives_a_run_over_the_real_mcp_bridge(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provision, implement, complete the step, review, and stop for a human."""
    repository = _repository(tmp_path)
    app, capabilities, gh_log = _compose(
        tmp_path, repository, monkeypatch, SCRIPTED_RUN
    )

    try:
        run, threads = asyncio.run(
            _drive(app, repository, TASK, provider, "awaiting_human_review")
        )
    finally:
        capabilities.state_store.close()

    assert run["phase"] == "awaiting_human_review", run["failureReason"]
    assert run["currentStepId"] == "human-review"
    assert run["name"] == SCRIPTED_RUN["title"]
    steps = {step["stepId"]: step for step in run["steps"]}
    # Nothing but a `complete_step` call over MCP could have produced these:
    # a step whose agent merely stops is corrected and asked again.
    assert steps["implementation"]["outputs"] == [
        {"name": "pr_url", "value": PULL_REQUEST}
    ]
    assert steps["review"]["outputs"] == [{"name": "findings", "value": FINDING}]
    assert run["pendingHumanReview"] is not None

    # Both steps share the run's one checkout, and the command really ran in it.
    workspaces = {thread["workspaceRoot"] for thread in threads}
    assert len(workspaces) == 1
    checkout = Path(workspaces.pop())
    assert (checkout / "greeting.txt").read_text().strip() == "hello"

    # And the reviewer's inline finding reached `gh` rather than GitHub.
    recorded = [call["argv"] for call in github_fakes.calls(gh_log)]
    assert recorded[0] == [
        "pr",
        "view",
        PULL_REQUEST,
        "--json",
        "headRefOid",
        "--jq",
        ".headRefOid",
    ]
    assert recorded[1] == [
        "api",
        "--method",
        "POST",
        "repos/acme/api/pulls/7/comments",
        "--raw-field",
        f"body={FINDING}",
        "--raw-field",
        f"commit_id={github_fakes.HEAD_SHA}",
        "--raw-field",
        "path=greeting.txt",
        "--field",
        "line=1",
        "--raw-field",
        "side=RIGHT",
    ]
    assert len(recorded) == 2


#: A completion that leaves out a declared output is not a completion. The
#: runtime says so and asks again, and the run fails rather than advancing on a
#: result the human decision would have been missing half of.
INCOMPLETE_RUN = {
    "title": "Adding a greeting",
    "scenarios": [
        {
            "when": "greeting",
            "steps": [
                {
                    "type": "tool",
                    "name": "complete_step",
                    "arguments": {
                        "outcome": "success",
                        "summary": "Added the greeting.",
                        "outputs": {},
                    },
                },
                # A turn ends in what the agent has to say for itself, and this
                # one has to say that its call was refused.
                {"type": "say", "text": "The completion was refused."},
            ],
        }
    ],
}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_completion_without_its_declared_output_is_refused(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    app, capabilities, _ = _compose(
        tmp_path, repository, monkeypatch, INCOMPLETE_RUN
    )

    try:
        run, _threads = asyncio.run(
            _drive(app, repository, TASK, provider, "failed")
        )
        said = asyncio.run(_said(capabilities.state_store, RunId(run["runId"])))
    finally:
        capabilities.state_store.close()

    assert run["phase"] == "failed"
    assert "without reporting a valid terminal result" in run["failureReason"]
    # The refusal is put to the agent rather than only written down: it comes
    # back as that tool call's own result, which is how a real one would learn
    # of it, and the correction that follows is the runtime asking again.
    assert any("missing required outputs: pr_url" in said_text for said_text in said)
    assert any(INVALID_COMPLETION_ERROR in said_text for said_text in said)
    steps = {step["stepId"]: step for step in run["steps"]}
    assert steps["implementation"]["outputs"] == []
    assert steps["review"]["status"] == "pending"
