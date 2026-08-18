"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
import json
from collections.abc import Mapping, Sequence

import httpx
import pytest

from engine.adapters.agent_runner.claude_code import ClaudeCodeAgentRunner
from engine.adapters.agent_runner.codex import (
    INTERACTIVE_APPROVAL_POLICY,
    CodexAgentRunner,
)
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import ThreadService, create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_runners,
    build_workflow_runners,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    ConversationId,
    HumanReviewCompleted,
    Message,
    Role,
    RunFailed,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    StepCompleted,
    StepOutput,
    TaskId,
    ToolCall,
    WorkspaceProvisioned,
)
from engine.core.workflows.implementation_review import (
    HUMAN_REVIEW_STEP,
    IMPLEMENTATION_STEP,
    REVIEW_STEP,
)
from engine.ports import (
    AgentTurn,
    InteractiveAgentRunner,
    McpServerConfig,
    Workspace,
    WorkspaceState,
)
from engine.runtime import AgentSession, Capabilities, INVALID_COMPLETION_ERROR
from engine.runtime.terminal_mcp import _mcp_response

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Be terse.",
        description="Reads code.",
    )
}


def test_web_composes_the_sqlite_conversation_store(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"

    capabilities = build_capabilities(Settings(sqlite_path=str(database)))

    assert isinstance(capabilities.state_store, SQLiteStateStore)
    assert database.exists()
    capabilities.state_store.close()


def test_web_offers_one_interactive_runner_per_cli() -> None:
    runners = build_runners(Settings())

    assert tuple(runners) == ("codex", "claude")
    assert isinstance(runners["codex"], CodexAgentRunner)
    assert isinstance(runners["claude"], ClaudeCodeAgentRunner)
    # Which of them pause is what decides whether a run brokers approvals, so
    # it is read off the port rather than off the class name.
    assert isinstance(runners["codex"], InteractiveAgentRunner)
    assert isinstance(runners["claude"], InteractiveAgentRunner)


def test_the_runner_nobody_is_watching_stays_read_only(tmp_path) -> None:
    """One class serves both callers now, so the sandbox is the whole difference.

    `build_runners` widens it because someone is there to approve; the port
    implementation a non-interactive caller reaches for has nobody to ask, and
    must not have been widened along with it.
    """
    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        argv = capabilities.agent_runner.command_line(PROFILES[CODER])
    finally:
        capabilities.state_store.close()

    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_interactive_runners_may_do_what_the_user_approves() -> None:
    """A gate is only a gate if what it lets through can then happen."""
    runners = build_runners(Settings())

    codex_argv = runners["codex"].command_line(PROFILES[CODER])
    claude_argv = runners["claude"].interactive_command_line(PROFILES[CODER])
    preapproved = claude_argv[
        claude_argv.index("--allowedTools") + 1 : claude_argv.index("--input-format")
    ]

    # Codex: writable inside the worktree, and stopping to ask before it would
    # step outside one.
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "workspace-write"
    assert INTERACTIVE_APPROVAL_POLICY == "on-request"
    # Claude: reads run unattended, everything else reaches the user.
    assert preapproved == ["Read", "Glob", "Grep"]
    assert "Bash" not in preapproved
    assert "Edit" not in preapproved
    assert claude_argv[claude_argv.index("--permission-prompt-tool") + 1] == "stdio"


def test_workflow_runners_are_write_enabled_only_inside_the_worktree() -> None:
    runners = build_workflow_runners(Settings())

    codex_argv = runners["codex"].command_line(PROFILES[CODER])
    claude_argv = runners["claude"].command_line(PROFILES[CODER])
    claude_tools = claude_argv[claude_argv.index("--allowedTools") + 1 :]

    assert codex_argv[codex_argv.index("--sandbox") + 1] == "workspace-write"
    assert claude_tools == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert "Bash" not in claude_tools


def test_every_workflow_runner_is_reviewed_by_a_read_only_runner_of_its_name() -> None:
    """What keeps a review read-only is the runner it gets, not its prompt."""
    settings = Settings()
    workflow_runners = build_workflow_runners(settings)
    reviewers = build_runners(settings)

    assert set(workflow_runners) <= set(reviewers)
    codex_argv = reviewers["codex"].command_line(PROFILES[CODER])
    claude_argv = reviewers["claude"].command_line(PROFILES[CODER])

    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"
    assert claude_argv[claude_argv.index("--allowedTools") + 1 :] == [
        "Read",
        "Glob",
        "Grep",
    ]


def test_web_restores_sqlite_conversations_after_restart(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"
    runner = ConcurrentRunner()
    other_runner = ConcurrentRunner(("persisted answer",))
    runners = {"test": runner, "other": other_runner}

    first_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    first_app = create_app(
        AgentSession(first_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def first_process() -> str:
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "remember this", "runner": "other"},
            )
            renamed = await client.patch(
                f"/api/threads/{thread_id}",
                json={"title": "Persistent metadata"},
            )
            archived = await client.post(f"/api/threads/{thread_id}/archive")
            assert renamed.status_code == 200
            assert archived.status_code == 200
            return thread_id

    thread_id = asyncio.run(first_process())
    first_capabilities.state_store.close()

    second_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    second_app = create_app(
        AgentSession(second_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def second_process():
        transport = httpx.ASGITransport(app=second_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            threads = await client.get("/api/threads")
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return threads, messages

    try:
        threads, messages = asyncio.run(second_process())
    finally:
        second_capabilities.state_store.close()

    assert threads.json()["threads"] == [
        {
            "id": thread_id,
            "title": "Persistent metadata",
            "archived": True,
            "agentId": "coder",
            "runner": "other",
            "workspaceAttached": False,
        }
    ]
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [("user", "remember this"), ("assistant", "persisted answer")]


class ConcurrentRunner:
    """A controllably slow runner that records how much work overlaps."""

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[Message, ...]] = []
        self.workspace_ids: list[str | None] = []
        self.active = 0
        self.most_active = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        reply = self.replies.pop(0) if self.replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


def _rejected(acknowledgement: dict[str, object] | None) -> bool:
    """Whether the broker refused a terminal tool call instead of accepting it."""
    result = (acknowledgement or {}).get("result")
    return isinstance(result, dict) and result.get("isError") is True


class TerminalToolRunner(ConcurrentRunner):
    """Call one workflow terminal tool through the attached MCP server."""

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments
        self.cancelled = asyncio.Event()
        self.acknowledgement: dict[str, object] | None = None

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        host = mcp_server.args[mcp_server.args.index("--host") + 1]
        port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
        token = mcp_server.args[mcp_server.args.index("--token") + 1]
        self.acknowledgement = await _mcp_response(
            host,
            port,
            token,
            {
                "jsonrpc": "2.0",
                "id": "workflow-tool-call-1",
                "method": "tools/call",
                "params": {
                    "name": self.tool_name,
                    "arguments": self.arguments,
                },
            },
        )
        if _rejected(self.acknowledgement):
            # A refused tool call leaves the CLI running, so it ends its turn
            # the way any provider would. Only an accepted result is followed
            # by the runtime cancelling the process.
            return AgentTurn(Message.assistant("The terminal call was refused."))
        await self.cancelled.wait()
        return AgentTurn(Message.assistant("Terminal result accepted."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        self.cancelled.set()


class InvalidThenTerminalToolRunner(TerminalToolRunner):
    """Exit once without a valid call, then complete through MCP."""

    def __init__(self, arguments: dict[str, object]) -> None:
        super().__init__("complete_step", arguments)
        self.attempts = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.attempts += 1
        if self.attempts == 1:
            self.seen.append(tuple(messages))
            self.workspace_ids.append(workspace_id)
            return AgentTurn(
                Message.assistant(
                    '{"outcome":"success","summary":"Legacy response","outputs":{}}'
                )
            )
        return await super().run_turn_with_mcp(
            agent_run_id,
            profile,
            messages,
            mcp_server,
            workspace_id,
        )


class ClarificationToolRunner(ConcurrentRunner):
    """Request user input instead of completing the bound step."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id=None,
    ) -> AgentTurn:
        self.attempts += 1
        question = ToolCall(
            "question-1",
            "AskUserQuestion",
            json.dumps({"question": "Which behavior should remain compatible?"}),
        )
        return AgentTurn(
            Message.assistant("Waiting for clarification."),
            steps=(Message.assistant(tool_calls=(question,)),),
        )


def _session(runner: ConcurrentRunner) -> AgentSession:
    return _session_with({"test": runner})


def _session_with(runners: Mapping[str, ConcurrentRunner]) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=next(iter(runners.values())),
            communications=unused,
            workspace_provider=unused,
            state_store=InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners=dict(runners),
    )


def _workflow_state(phase: RunPhase) -> RunState:
    run_id = RunId(f"run-{phase.value}")
    implementation = StepCompleted(
        run_id=run_id,
        step_id=IMPLEMENTATION_STEP,
        agent_run_id=AgentRunId("implementation-execution"),
        outcome="success",
        summary="Implemented the lock and regression test.",
        outputs=(StepOutput("changed_files", "worker.py, test_worker.py"),),
    )
    review = StepCompleted(
        run_id=run_id,
        step_id=REVIEW_STEP,
        agent_run_id=AgentRunId("review-execution"),
        outcome="changes_requested",
        summary="The test should also cover cancellation.",
        outputs=(StepOutput("findings", "Cancellation coverage missing"),),
    )
    values = {
        RunPhase.IMPLEMENTING: (IMPLEMENTATION_STEP, (), None, ""),
        RunPhase.REVIEWING: (REVIEW_STEP, (implementation,), None, ""),
        RunPhase.AWAITING_HUMAN_REVIEW: (
            HUMAN_REVIEW_STEP,
            (implementation, review),
            None,
            "",
        ),
        RunPhase.SUCCEEDED: (
            HUMAN_REVIEW_STEP,
            (implementation, review),
            HumanReviewCompleted(
                run_id=run_id,
                step_id=HUMAN_REVIEW_STEP,
                approved=True,
                summary="The residual risk is acceptable.",
            ),
            "",
        ),
        RunPhase.FAILED: (
            IMPLEMENTATION_STEP,
            (
                StepCompleted(
                    run_id=run_id,
                    step_id=IMPLEMENTATION_STEP,
                    agent_run_id=AgentRunId("implementation-execution"),
                    outcome="failed",
                    summary="Tests did not pass.",
                ),
            ),
            None,
            "Tests did not pass.",
        ),
    }
    current_step, results, decision, reason = values[phase]
    return RunState(
        run_id=run_id,
        task_id=TaskId("task-42"),
        phase=phase,
        repository="acme/api",
        prompt="Fix the race and add a regression test.",
        current_step_id=current_step,
        current_agent_run_id=(
            AgentRunId(f"current-{current_step}")
            if current_step != HUMAN_REVIEW_STEP
            else None
        ),
        step_results=results,
        human_review=decision,
        failure_reason=reason,
    )


def _reviewer(
    summary: str = "The change matches the task.",
    findings: str = "No blocking findings.",
) -> TerminalToolRunner:
    """A reviewer that completes its step with the output the step declares."""
    return TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": summary,
            "outputs": {"findings": findings},
        },
    )


def _workflow_app(
    store: InMemoryStateStore,
    runner: ConcurrentRunner,
    workspaces: object | None = None,
    workflow_runners: dict[str, ConcurrentRunner] | None = None,
    reviewers: dict[str, ConcurrentRunner] | None = None,
):
    """Wire the app the way the composition root does.

    `workflow_runners` implement and may write; the chat runner registered under
    the same name reviews, and is the one that may not.
    """
    unused = object()
    implementers = dict(workflow_runners or {"test": runner})
    chat_runners: dict[str, ConcurrentRunner] = dict(
        reviewers or {name: _reviewer() for name in implementers}
    )
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces or ConversationWorkspaces(),
            state_store=store,
        ),
        profiles=PROFILES,
        runners=chat_runners,
    )
    return create_app(session, chat_runners, workflow_runners=implementers)


async def _await_phase(
    client: httpx.AsyncClient, run_id: RunId, phase: str
) -> httpx.Response:
    """Poll a run until it reaches `phase`, or return the last view it had."""
    for _ in range(200):
        response = await client.get(f"/api/runs/{run_id}")
        if response.json()["phase"] == phase:
            return response
        await asyncio.sleep(0.01)
    return response


@pytest.mark.parametrize(
    ("phase", "terminal_outcome"),
    [
        (RunPhase.IMPLEMENTING, None),
        (RunPhase.REVIEWING, None),
        (RunPhase.AWAITING_HUMAN_REVIEW, None),
        (RunPhase.SUCCEEDED, "approved"),
        (RunPhase.FAILED, "failed"),
    ],
)
def test_run_api_covers_workflow_lifecycle_phases(
    phase: RunPhase, terminal_outcome: str | None
) -> None:
    store = InMemoryStateStore()
    state = _workflow_state(phase)
    asyncio.run(store.save(state))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/runs")
            detail = await client.get(f"/api/runs/{state.run_id}")
            return listed, detail

    listed, detail = asyncio.run(scenario())
    body = detail.json()

    assert listed.status_code == 200
    assert [run["runId"] for run in listed.json()["runs"]] == [state.run_id]
    assert detail.status_code == 200
    assert body["taskPrompt"] == "Fix the race and add a regression test."
    assert body["workflowId"] == "implementation-review-v1"
    assert body["phase"] == phase.value
    assert body["currentStepId"] == state.current_step_id
    assert body["terminalOutcome"] == terminal_outcome
    assert [step["stepId"] for step in body["steps"]] == [
        IMPLEMENTATION_STEP,
        REVIEW_STEP,
        HUMAN_REVIEW_STEP,
    ]

    if phase is RunPhase.AWAITING_HUMAN_REVIEW:
        assert body["pendingHumanReview"] is not None
        assert "Implemented the lock" in body["pendingHumanReview"]["summary"]
        assert body["steps"][1]["changesRequested"] is True
        assert body["humanDecision"] is None
    if phase is RunPhase.SUCCEEDED:
        assert body["humanDecision"] == {
            "stepId": "human-review",
            "approved": True,
            "outcome": "approved",
            "summary": "The residual risk is acceptable.",
        }
        assert body["steps"][1]["outcome"] == "changes_requested"


def test_create_workflow_run_implements_reviews_and_awaits_a_human() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Added cancellation handling.",
            "outputs": {},
        },
    )
    reviewer = _reviewer(
        summary="The handling is correct.",
        findings="worker.py cancels the task, and the new test covers it.",
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Add cancellation handling.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            listed = await client.get("/api/runs")
            instances = await store.list_instances(workflow_run_id=run_id)
            conversations = {
                instance.workflow_step_id: await store.load_conversation(
                    instance.instance_id
                )
                for instance in instances
            }
            return created, reopened, listed, await store.history(run_id), conversations

    created, reopened, listed, history, conversations = asyncio.run(scenario())
    body = reopened.json()

    assert created.status_code == 201
    assert created.json()["phase"] == "pending"
    assert created.json()["currentStepId"] is None
    assert body["phase"] == "awaiting_human_review"
    assert body["currentStepId"] == "human-review"
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][0]["summary"] == "Added cancellation handling."
    assert body["steps"][0]["conversationUrl"]
    assert body["steps"][1]["status"] == "completed"
    assert body["steps"][1]["outcome"] == "success"
    assert body["steps"][1]["summary"] == "The handling is correct."
    assert body["steps"][1]["outputs"] == [
        {"name": "findings", "value": "worker.py cancels the task, and the new test covers it."}
    ]
    assert body["steps"][1]["conversationUrl"]
    assert body["steps"][2]["status"] == "action_required"
    assert "worker.py cancels the task" in body["pendingHumanReview"]["summary"]
    assert [run["runId"] for run in listed.json()["runs"]] == [
        created.json()["runId"]
    ]
    assert len(history) == 4
    assert isinstance(history[0], RunRequested)
    assert isinstance(history[1], WorkspaceProvisioned)
    assert isinstance(history[2], StepCompleted)
    assert isinstance(history[3], StepCompleted)
    assert history[0].prompt == "Add cancellation handling."
    assert history[0].repository == "acme/api"
    assert (history[2].step_id, history[3].step_id) == (IMPLEMENTATION_STEP, REVIEW_STEP)
    # Two executions, one each, in the single workspace the run provisioned.
    assert implementer.workspace_ids == ["ws-1"]
    assert reviewer.workspace_ids == ["ws-1"]
    # Two conversations, kept apart: the review is its own durable instance.
    assert set(conversations) == {IMPLEMENTATION_STEP, REVIEW_STEP}
    assert all(conversation.messages for conversation in conversations.values())
    assert "`complete_step`" in implementer.seen[0][0].content
    assert "JSON" not in implementer.seen[0][0].content


def test_the_reviewer_reads_the_task_and_the_implementation_result() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Took the lock around the shared counter.",
            "outputs": {},
        },
    )
    reviewer = _reviewer()
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Fix the race and add a regression test.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            return await _await_phase(client, run_id, "awaiting_human_review")

    reopened = asyncio.run(scenario())
    prompt = reviewer.seen[0][0].content

    assert reopened.json()["phase"] == "awaiting_human_review"
    assert "Fix the race and add a regression test." in prompt
    assert "Took the lock around the shared counter." in prompt
    assert "Outcome: success" in prompt
    assert "do not modify" in prompt
    # The step declares an output, so the reviewer is told to report one.
    assert "findings" in prompt
    # One turn each: the write-enabled runner implemented and did not review.
    assert len(implementer.seen) == 1
    assert len(reviewer.seen) == 1


def test_complete_step_mcp_call_completes_the_active_workflow_step() -> None:
    store = InMemoryStateStore()
    runner = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Completed through MCP.",
            "outputs": {},
        },
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Complete the implementation through MCP.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "awaiting_human_review"
    completed_step = reopened.json()["steps"][0]
    assert completed_step["status"] == "completed"
    assert completed_step["outcome"] == "success"
    assert completed_step["summary"] == "Completed through MCP."
    assert completed_step["outputs"] == []
    assert completed_step["mcpRequestId"] == "workflow-tool-call-1"
    implementation = history[2]
    assert isinstance(implementation, StepCompleted)
    assert implementation.step_id == IMPLEMENTATION_STEP
    assert implementation.mcp_request_id == "workflow-tool-call-1"
    assert runner.cancelled.is_set()
    assert runner.acknowledgement == {
        "jsonrpc": "2.0",
        "id": "workflow-tool-call-1",
        "result": {
            "content": [{"type": "text", "text": "accepted"}],
            "structuredContent": {"accepted": True},
        },
    }


def test_invalid_exit_is_retried_and_then_completes_the_active_step() -> None:
    store = InMemoryStateStore()
    runner = InvalidThenTerminalToolRunner(
        {
            "outcome": "success",
            "summary": "Completed after correction.",
            "outputs": {},
        }
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Complete after an invalid exit.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            return await _await_phase(client, run_id, "awaiting_human_review")

    reopened = asyncio.run(scenario())

    assert reopened.json()["phase"] == "awaiting_human_review"
    assert reopened.json()["steps"][0]["summary"] == "Completed after correction."
    assert runner.attempts == 2
    assert runner.seen[1][-1] == Message.user(INVALID_COMPLETION_ERROR)


def test_clarification_call_leaves_the_active_step_implementing() -> None:
    store = InMemoryStateStore()
    runner = ClarificationToolRunner()
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Ask when a requirement is ambiguous.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(20):
                state = await store.load(run_id)
                if state is not None and state.current_agent_run_id is not None:
                    agent_run = await store.agent_run(state.current_agent_run_id)
                    if (
                        agent_run is not None
                        and agent_run.status is AgentRunStatus.SUCCEEDED
                    ):
                        break
                await asyncio.sleep(0.01)
            return await client.get(f"/api/runs/{run_id}"), await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "implementing"
    assert reopened.json()["currentStepId"] == "implementation"
    assert runner.attempts == 1
    assert not any(isinstance(event, (StepCompleted, RunFailed)) for event in history)


def test_fail_step_mcp_call_fails_the_active_workflow() -> None:
    store = InMemoryStateStore()
    runner = TerminalToolRunner(
        "fail_step",
        {"summary": "The implementation cannot continue."},
    )
    app = _workflow_app(store, runner)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Attempt the implementation through MCP.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            for _ in range(20):
                reopened = await client.get(f"/api/runs/{run_id}")
                if reopened.json()["phase"] == "failed":
                    break
                await asyncio.sleep(0.01)
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())

    assert reopened.json()["phase"] == "failed"
    assert reopened.json()["terminalOutcome"] == "failed"
    assert reopened.json()["failureReason"] == "The implementation cannot continue."
    assert reopened.json()["steps"][0]["status"] == "failed"
    assert isinstance(history[-1], RunFailed)
    assert history[-1].agent_run_id is not None
    assert history[-1].mcp_request_id == "workflow-tool-call-1"
    assert runner.cancelled.is_set()
    assert runner.acknowledgement is not None
    assert runner.acknowledgement["result"] == {
        "content": [{"type": "text", "text": "accepted"}],
        "structuredContent": {"accepted": True},
    }


def test_a_failing_reviewer_fails_the_run_after_a_successful_implementation() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented the change.",
            "outputs": {},
        },
    )
    reviewer = TerminalToolRunner(
        "fail_step",
        {"summary": "The workspace no longer holds the implemented change."},
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement a change the reviewer cannot inspect.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "failed")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())
    body = reopened.json()

    assert body["phase"] == "failed"
    assert body["terminalOutcome"] == "failed"
    assert body["failureReason"] == (
        "The workspace no longer holds the implemented change."
    )
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][1]["status"] == "failed"
    assert body["pendingHumanReview"] is None
    assert isinstance(history[-1], RunFailed)
    assert history[-1].agent_run_id is not None
    assert reviewer.cancelled.is_set()


def test_a_reviewer_that_omits_a_declared_output_fails_the_run() -> None:
    store = InMemoryStateStore()
    implementer = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented the change.",
            "outputs": {},
        },
    )
    # Valid for the implementation step, which declares no outputs, and not for
    # the review step, which declares `findings`.
    reviewer = TerminalToolRunner(
        "complete_step",
        {"outcome": "success", "summary": "Looks fine.", "outputs": {}},
    )
    app = _workflow_app(store, implementer, reviewers={"test": reviewer})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement a change the reviewer reports badly.",
                    "repository": "acme/api",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "failed")
            return reopened, await store.history(run_id)

    reopened, history = asyncio.run(scenario())
    body = reopened.json()

    assert body["phase"] == "failed"
    assert body["steps"][0]["status"] == "completed"
    assert body["steps"][1]["status"] == "failed"
    assert "terminal result" in body["failureReason"]
    assert REVIEW_STEP in body["failureReason"]
    assert isinstance(history[-1], RunFailed)
    # Corrected rather than accepted, and only up to the runtime's limit.
    assert len(reviewer.seen) == 3
    assert reviewer.seen[-1][-1] == Message.user(INVALID_COMPLETION_ERROR)
    assert reviewer.acknowledgement is not None
    assert reviewer.acknowledgement["result"] == {
        "content": [
            {
                "type": "text",
                "text": "step result is missing required outputs: findings",
            }
        ],
        "isError": True,
    }
    assert not reviewer.cancelled.is_set()


def test_create_workflow_run_uses_and_persists_the_selected_runner() -> None:
    store = InMemoryStateStore()
    codex = ConcurrentRunner()
    codex_reviewer = _reviewer()
    claude = TerminalToolRunner(
        "complete_step",
        {
            "outcome": "success",
            "summary": "Implemented with Claude.",
            "outputs": {},
        },
    )
    claude_reviewer = _reviewer(summary="Reviewed with Claude.")
    app = _workflow_app(
        store,
        codex,
        workflow_runners={"codex": codex, "claude": claude},
        reviewers={"codex": codex_reviewer, "claude": claude_reviewer},
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/runs",
                json={
                    "workflowId": "implementation-review-v1",
                    "prompt": "Implement the feature.",
                    "repository": "acme/api",
                    "runner": "claude",
                },
            )
            run_id = RunId(created.json()["runId"])
            reopened = await _await_phase(client, run_id, "awaiting_human_review")
            return reopened, [
                await store.load_instance(AgentInstanceId(step["agentInstanceId"]))
                for step in reopened.json()["steps"][:2]
            ]

    reopened, instances = asyncio.run(scenario())

    assert reopened.json()["steps"][0]["summary"] == "Implemented with Claude."
    assert reopened.json()["steps"][1]["summary"] == "Reviewed with Claude."
    # The provider a run picks answers for both steps, and the other one for
    # neither -- write-enabled for the implementation, read-only for the review.
    assert len(claude.seen) == 1
    assert len(claude_reviewer.seen) == 1
    assert codex.seen == []
    assert codex_reviewer.seen == []
    assert [instance.runner for instance in instances] == ["claude", "claude"]


@pytest.mark.parametrize(
    "body",
    [
        {"workflowId": "unknown-v1", "prompt": "Task", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "Task"},
        {
            "workflowId": "implementation-review-v1",
            "prompt": "Task",
            "repository": ".",
            "runner": "unknown",
        },
    ],
)
def test_create_workflow_run_rejects_invalid_requests(body: dict[str, str]) -> None:
    store = InMemoryStateStore()
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/runs", json=body)

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert asyncio.run(store.list_runs()) == ()


def test_workflow_conversation_is_nested_under_its_run_not_standalone() -> None:
    store = InMemoryStateStore()
    state = _workflow_state(RunPhase.REVIEWING)
    asyncio.run(store.save(state))
    asyncio.run(
        store.create_instance(
            AgentId("implementation-agent"),
            instance_id=AgentInstanceId("implementation-instance"),
            conversation_id=ConversationId("implementation-conversation"),
            workflow_run_id=state.run_id,
            workflow_step_id=IMPLEMENTATION_STEP,
        )
    )
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            runs = await client.get(f"/api/runs/{state.run_id}")
            threads = await client.get("/api/threads")
            thread = await client.get("/api/threads/implementation-instance")
            return runs, threads, thread

    runs, threads, thread = asyncio.run(scenario())

    implementation = runs.json()["steps"][0]
    assert implementation["agentInstanceId"] == "implementation-instance"
    assert implementation["conversationId"] == "implementation-conversation"
    assert implementation["conversationUrl"] == "/conversations/implementation-instance"
    assert threads.json() == {"threads": []}
    assert thread.json()["workflowRunId"] == state.run_id
    assert thread.json()["workflowStepId"] == IMPLEMENTATION_STEP


def test_run_api_presents_human_rejection_as_the_final_decision() -> None:
    store = InMemoryStateStore()
    awaiting = _workflow_state(RunPhase.AWAITING_HUMAN_REVIEW)
    rejection = HumanReviewCompleted(
        run_id=awaiting.run_id,
        step_id=HUMAN_REVIEW_STEP,
        approved=False,
        summary="Address the cancellation finding first.",
    )
    rejected = RunState(
        run_id=awaiting.run_id,
        task_id=awaiting.task_id,
        workflow_id=awaiting.workflow_id,
        phase=RunPhase.FAILED,
        repository=awaiting.repository,
        prompt=awaiting.prompt,
        current_step_id=HUMAN_REVIEW_STEP,
        step_results=awaiting.step_results,
        human_review=rejection,
    )
    asyncio.run(store.save(rejected))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(f"/api/runs/{rejected.run_id}")

    body = asyncio.run(scenario()).json()

    assert body["terminalOutcome"] == "rejected"
    assert body["humanDecision"]["outcome"] == "rejected"
    assert body["humanDecision"]["summary"] == rejection.summary
    assert body["steps"][1]["outcome"] == "changes_requested"


def test_run_id_frontend_route_serves_the_application(tmp_path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/runs/run-42")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


def test_new_workflow_frontend_route_serves_the_application(tmp_path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/runs/new")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


class ConversationWorkspaces:
    """A provider whose checkouts come and go, as real ones do."""

    def __init__(self) -> None:
        self.count = 0
        self.detached: set[str] = set()

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        self.count += 1
        return self._workspace(f"ws-{self.count}", repository, base_ref)

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.detached:
            raise KeyError(f"no workspace {workspace_id!r}")
        return f"/worktrees/{workspace_id}"

    async def state(self, workspace_id: str) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=(
                None if workspace_id in self.detached else f"/worktrees/{workspace_id}"
            ),
        )

    async def attach(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        self.detached.discard(workspace_id)
        return self._workspace(workspace_id, repository, base_ref)

    async def detach(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    async def dispose(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    def _workspace(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/worktrees/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )


class VanishingWorkspaces(ConversationWorkspaces):
    """A provider that has never heard of a workspace the store still names."""

    def __init__(self) -> None:
        super().__init__()
        self.forgotten: set[str] = set()

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().root_path(workspace_id)

    async def state(self, workspace_id: str) -> WorkspaceState:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().state(workspace_id)


def _workspace_session(
    runner: ConcurrentRunner,
    workspaces: ConversationWorkspaces,
    store: InMemoryStateStore | None = None,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces,
            state_store=store if store is not None else InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners={"test": runner},
        workspace_repository="/repository",
    )


def test_each_new_chat_reports_its_own_worktree() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    session = _workspace_session(runner, workspaces)
    app = create_app(session, {"test": runner})

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"agentId": "coder", "runner": "test"}
            first = await client.post("/api/threads", json=body)
            second = await client.post("/api/threads", json=body)
            await client.post(
                f"/api/threads/{first.json()['id']}/runs", json={"text": "inspect"}
            )
            return first.json(), second.json()

    first, second = asyncio.run(scenario())

    assert first["workspaceRoot"] == "/worktrees/ws-1"
    assert second["workspaceRoot"] == "/worktrees/ws-2"
    assert first["workspaceRoot"] != second["workspaceRoot"]
    assert runner.workspace_ids == ["ws-1"]


def test_a_removed_worktree_does_not_take_the_other_chats_with_it() -> None:
    """One vanished checkout used to brick every endpoint, new chats included."""
    runner = ConcurrentRunner()
    workspaces = VanishingWorkspaces()
    store = InMemoryStateStore()
    first_app = create_app(
        _workspace_session(runner, workspaces, store), {"test": runner}
    )

    async def scenario():
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            abandoned = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
        workspaces.forgotten.add("ws-1")

        # A restart: the registry is rebuilt from the store, whose instances
        # still name a workspace that is no longer on disk.
        restarted = create_app(
            _workspace_session(runner, workspaces, store), {"test": runner}
        )
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/threads")
            survivor = await client.get(f"/api/threads/{abandoned.json()['id']}")
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            fresh = await client.get(f"/api/threads/{created.json()['id']}")
        return listed, survivor, created, fresh

    listed, survivor, created, fresh = asyncio.run(scenario())

    assert listed.status_code == 200
    assert survivor.status_code == 200
    assert "workspaceRoot" not in survivor.json()
    assert survivor.json()["workspaceAttached"] is False
    assert created.status_code == 201
    assert fresh.status_code == 200
    assert fresh.json()["workspaceRoot"] == "/worktrees/ws-2"


def test_detaching_keeps_the_work_reachable_and_reattaching_brings_it_back() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = create_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            detached = await client.delete(f"/api/threads/{thread_id}/workspace")
            listed_detached = await client.get(f"/api/threads/{thread_id}")
            reattached = await client.post(f"/api/threads/{thread_id}/workspace")
        return created.json(), detached.json(), listed_detached.json(), reattached.json()

    created, detached, listed, reattached = asyncio.run(scenario())

    assert created["workspaceAttached"] is True
    assert detached["workspaceAttached"] is False
    assert "workspaceRoot" not in detached
    # The work stays addressable while there is nowhere to run it.
    assert detached["workspaceRef"] == "engine/ws-1"
    assert listed["workspaceAttached"] is False
    # Reattaching is the same workspace, not a replacement for it.
    assert reattached["workspaceAttached"] is True
    assert reattached["workspaceRoot"] == created["workspaceRoot"]
    assert reattached["workspaceRef"] == "engine/ws-1"
    assert workspaces.count == 1


def test_a_detached_chat_is_told_to_reattach_rather_than_failing_on_a_path() -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = create_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.delete(f"/api/threads/{thread_id}/workspace")
            refused = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
            await client.post(f"/api/threads/{thread_id}/workspace")
            accepted = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
        return refused, accepted

    refused, accepted = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "reattach" in refused.json()["error"]
    assert accepted.status_code == 200
    assert runner.workspace_ids == ["ws-1"]


def test_a_chat_that_never_had_a_workspace_can_be_given_one() -> None:
    """Conversations from before worktrees existed, and any other stragglers."""
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    store = InMemoryStateStore()
    session = _workspace_session(runner, workspaces, store)
    app = create_app(session, {"test": runner})

    async def scenario():
        instance = await store.create_instance(CODER)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            before = await client.get(f"/api/threads/{instance.instance_id}")
            attached = await client.post(f"/api/threads/{instance.instance_id}/workspace")
        # The pairing is durable, not just something the page is holding.
        stored = await store.load_instance(instance.instance_id)
        return before.json(), attached.json(), stored

    before, attached, stored = asyncio.run(scenario())

    assert before["workspaceAttached"] is False
    assert "workspaceRef" not in before
    assert attached["workspaceAttached"] is True
    assert attached["workspaceRoot"] == "/worktrees/ws-1"
    assert stored.workspace_id == "ws-1"


def test_a_process_without_a_workspace_repository_says_so() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            return await client.post(f"/api/threads/{created.json()['id']}/workspace")

    refused = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "workspace repository" in refused.json()["error"]


def test_different_chats_can_run_at_the_same_time() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> None:
        first = await service.create(CODER, "test")
        second = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(first.instance_id, "first", None, asyncio.Queue()),
            service.say(second.instance_id, "second", None, asyncio.Queue()),
        )

    asyncio.run(scenario())

    assert runner.most_active == 2


def test_one_chat_serializes_its_own_turns() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> tuple[Message, ...]:
        thread = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(thread.instance_id, "first", None, asyncio.Queue()),
            service.say(thread.instance_id, "second", None, asyncio.Queue()),
        )
        return await service.history(thread.instance_id)

    history = asyncio.run(scenario())

    assert runner.most_active == 1
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "first"),
        (Role.ASSISTANT, "one"),
        (Role.USER, "second"),
        (Role.ASSISTANT, "two"),
    ]


def test_http_api_creates_lists_and_streams_threads() -> None:
    runner = ConcurrentRunner(("hello",))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            config = await client.get("/api/config")
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "hi", "runner": "test"},
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
        return config, created, streamed, messages

    config, created, streamed, messages = asyncio.run(scenario())

    assert config.status_code == 200
    assert config.json()["defaultRunner"] == "test"
    assert config.json()["workflowRunners"] == ["test"]
    assert config.json()["defaultWorkflowRunner"] == "test"
    assert created.status_code == 201
    assert streamed.status_code == 200
    assert '"type":"done"' in streamed.text
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_a_chat_keeps_the_runner_it_was_given_for_turns_that_name_none() -> None:
    """The conversation remembers its runner; a turn need not repeat it.

    The header sends the choice once, so a turn that carries no runner has to
    reach whoever the chat was last set to rather than the wired default.
    """
    first = ConcurrentRunner(("from the first",))
    second = ConcurrentRunner(("from the second",))
    runners = {"test": first, "other": second}
    app = create_app(_session_with(runners), runners)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            switched = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "other"}
            )
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "hi"})
            reloaded = await client.get(f"/api/threads/{thread_id}")
            unknown = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "nobody"}
            )
            return switched, reloaded, unknown

    switched, reloaded, unknown = asyncio.run(scenario())

    assert switched.json()["runner"] == "other"
    assert reloaded.json()["runner"] == "other"
    assert [turn[-1].content for turn in second.seen] == ["hi"]
    assert first.seen == []
    assert unknown.status_code == 400


def test_agent_names_chat_before_answer_without_changing_conversation() -> None:
    runner = ConcurrentRunner(('"SQLite Conversation Persistence"', "The answer."))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title",
                json={
                    "text": "Why are chats missing after restart?",
                    "runner": "test",
                },
            )
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "Why are chats missing after restart?"},
            )
            repeated_title = await client.post(
                f"/api/threads/{thread_id}/title", json={}
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return title, repeated_title, messages

    title, repeated_title, messages = asyncio.run(scenario())

    assert title.json() == {"title": "SQLite Conversation Persistence"}
    assert repeated_title.json() == title.json()
    assert runner.seen[0] == (
        Message.user("Why are chats missing after restart?"),
        Message.user(
            "Name this chat based on the conversation above. Reply with only a concise "
            "title of at most eight words, with no quotes or ending punctuation."
        ),
    )
    assert runner.seen[1] == (Message.user("Why are chats missing after restart?"),)
    assert len(runner.seen) == 2
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "Why are chats missing after restart?"),
        ("assistant", "The answer."),
    ]


def test_a_provider_that_cannot_name_a_chat_does_not_cost_the_turn() -> None:
    """Naming happens before the message it names is sent, so it cannot fail it.

    A CLI that is out of quota, unauthenticated, or simply broken fails the
    first thing the client asks of it, which is a title. Answered with a 500
    that would stop the chat working entirely -- for a name.
    """

    class FailsToName(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            if args[2][-1].content.startswith("Name this chat"):
                raise RuntimeError("codex exited 1: stream error: unauthorized")
            return await super().run_turn(*args, **kwargs)

    runner = FailsToName(("The answer.",))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title", json={"text": "hello"}
            )
            run = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "hello"}
            )
            return title, run, await client.get(f"/api/threads/{thread_id}")

    title, run, thread = asyncio.run(scenario())

    assert title.status_code == 200
    assert title.json()["title"] == "New chat"
    # Not silence: the placeholder name says nothing about which provider
    # failed, and somebody reading the response deserves the reason.
    assert "unauthorized" in title.json()["error"]
    # The turn the client was about to send goes through regardless.
    assert run.status_code == 200
    assert thread.json()["title"] == "New chat"
    finished = json.loads([line for line in run.text.splitlines() if line][-1])
    assert finished["type"] == "done"
    assert finished["content"][0]["text"] == "The answer."


def test_missing_frontend_has_an_actionable_response() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert "npm --prefix apps/web run build" in response.text


def test_tool_activity_round_trips_as_assistant_ui_parts() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class ToolRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ToolRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "inspect"})
            return (await client.get(f"/api/threads/{thread_id}/messages")).json()

    content = asyncio.run(scenario())["messages"][1]["content"]

    assert content == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
            "result": "engine",
        },
        {"type": "text", "text": "Found it."},
    ]


def test_active_run_survives_stream_disconnect_and_replays_progress() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class RefreshRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            tool_call = Message.assistant(tool_calls=(call,))
            tool_result = Message.tool_result(call.call_id, "engine")
            answer = Message.assistant("Found it.")
            on_message(tool_call)
            self.started.set()
            await self.release.wait()
            on_message(tool_result)
            on_message(answer)
            return AgentTurn(answer, steps=(tool_call, tool_result))

    runner = RefreshRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "inspect", None)
        await runner.started.wait()

        original_stream = run.stream()
        first = json.loads((await anext(original_stream)).decode())
        await original_stream.aclose()  # the browser refreshed

        assert service.active_run(thread.instance_id) is run
        active = service.active_run(thread.instance_id)
        assert active is not None
        resumed_stream = active.stream()
        replayed = json.loads((await anext(resumed_stream)).decode())

        runner.release.set()
        events = [replayed]
        async for event in resumed_stream:
            events.append(json.loads(event.decode()))
        return first, events, await service.history(thread.instance_id)

    first, events, history = asyncio.run(scenario())

    assert first["content"] == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
        }
    ]
    assert events[0] == first
    assert events[-1]["type"] == "done"
    assert events[-1]["content"][-1] == {"type": "text", "text": "Found it."}
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "inspect"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "engine"),
        (Role.ASSISTANT, "Found it."),
    ]


def test_the_built_client_is_revalidated_but_its_hashed_assets_are_not(tmp_path) -> None:
    """A cached entry point asks for the assets of a build that is gone."""
    runner = ConcurrentRunner()
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/index-abc123.js"></script>')
    (dist / "assets" / "index-abc123.js").write_text("console.log('engine')")
    app = create_app(_session(runner), {"test": runner}, dist)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                await client.get("/"),
                await client.get("/assets/index-abc123.js"),
            )

    page, asset = asyncio.run(scenario())

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
