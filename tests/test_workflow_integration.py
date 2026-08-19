"""Integration coverage for a complete implementation-review workflow."""

import asyncio
import subprocess
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import engine.runtime.dispatcher as dispatcher_module
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workspace_provider.git_worktree import (
    GitWorktreeWorkspaceProvider,
)
from engine.apps.web.api import create_app
from engine.domain import (
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    HumanReviewCompleted,
    Message,
    RunId,
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
from engine.runtime.step_results import step_completed_from_arguments
from engine.runtime.terminal_mcp import (
    TerminalDelivery,
    TerminalEvent,
    TerminalResultRegistry,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


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
        self.cancelled = asyncio.Event()

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        raise AssertionError("workflow execution must provide the MCP server")

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
    client: httpx.AsyncClient, run_id: RunId, phase: str
) -> httpx.Response:
    for _ in range(200):
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
            "outputs": {},
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
    assert reopened_state == state
    assert [type(event) for event in history] == [
        RunRequested,
        WorkspaceProvisioned,
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
    assert asyncio.run(workspaces.state(state.workspace_id)).attached
