"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from engine.adapters.agent_runner.claude_code import ClaudeCodeAgentRunner
from engine.adapters.agent_runner.codex import (
    INTERACTIVE_APPROVAL_POLICY,
    CodexAgentRunner,
)
from engine.adapters.communications.slack import SlackCommunications
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.__main__ import build_app
from engine.apps.web.api import ApprovalFeed, ThreadService, create_app
from engine.apps.web.utilization import (
    RunnerUtilization,
    UtilizationService,
    UtilizationWindow,
)
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_communications,
    build_milestone_scoper,
    build_read_only_runners,
    build_runners,
    build_session,
    claude_session_config_for,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    ApprovalDecision,
    ApprovalKind,
    Message,
    Milestone,
    MilestoneId,
    Project,
    ProjectId,
    Role,
    RunId,
    RunPhase,
    RunState,
    ScopingPlan,
    TaskId,
    ToolCall,
    WorkflowId,
    Workstream,
    WorkstreamId,
    WorkOrderId,
    WorkOrderSpec,
    WorkOrderStatus,
    project_id_for_instance,
)
from engine.ports import (
    AgentTurn,
    ApprovalRequest,
    InteractiveAgentRunner,
    McpServerConfig,
    Workspace,
    WorkspaceState,
)
from engine.runtime import (
    BUILT_IN,
    PLANNER,
    AgentSession,
    ApprovalBroker,
    ApprovalCapability,
    ApprovalConfig,
    Capabilities,
    ClaudeConfig,
    CommunicationsConfig,
    EngineConfig,
    ResponseStyle,
    WorkflowCatalog,
)
from engine.graph_runtime import (
    CANCELLED,
    CheckpointId,
    GraphCompilationError,
    GraphId,
    NodeId,
    RunSnapshot,
    RunStatus,
)
from graph_runtime_fakes import (
    Ask,
    AwaitSteering,
    Fail,
    Say,
    ScriptedGraph,
    ScriptedGraphRuntime,
    ScriptedNode,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR

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


def test_web_selects_the_configured_communications_provider() -> None:
    slack = build_communications(Settings())

    assert isinstance(slack, SlackCommunications)

    with pytest.raises(RuntimeError, match="provider 'buzz' is not available yet"):
        build_communications(
            Settings(
                engine_config=EngineConfig(
                    communications=CommunicationsConfig(provider="buzz")
                )
            )
        )


def test_the_application_can_be_built_from_configuration_alone(tmp_path, monkeypatch) -> None:
    """The contract the development server's reloader depends on.

    It constructs the application again in every child process it starts, with
    no command line and nothing handed to it, so a composition that only works
    when `main` assembles it would leave `engine-dev` reloading into nothing.
    """
    monkeypatch.chdir(tmp_path)

    app = build_app()

    async def ask() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/api/config")

    answered = asyncio.run(ask())
    assert answered.status_code == 200
    assert answered.json()["runners"] == [
        {"id": "codex", "implementation": "CodexAgentRunner"},
        {"id": "claude", "implementation": "ClaudeCodeAgentRunner"},
    ]
    # Composed from the working directory, exactly as `engine-web` composes it.
    assert (tmp_path / "conversations.sqlite3").exists()
    assert app.state.milestone_scoper is not None


def test_milestone_scoper_uses_the_configured_codex_provider() -> None:
    settings = Settings(
        codex_binary="/opt/openengine/codex",
        codex_working_directory="/srv/openengine/repository",
        codex_timeout_seconds=42,
        codex_model="gpt-scoper",
    )

    milestone_scoper = build_milestone_scoper(settings)
    provider = milestone_scoper.scoper.registry.resolve("codex")

    assert provider.env == {
        "CODEX_PATH": "/opt/openengine/codex",
        "CODEX_CONFIG": '{"model": "gpt-scoper"}',
    }
    assert provider.cwd == "/srv/openengine/repository"
    assert milestone_scoper.scoper.working_directory == "/srv/openengine/repository"
    assert milestone_scoper.scoper.timeout_seconds == 42


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


def test_the_configured_policy_builds_the_interactive_claude_runner() -> None:
    """`engine.toml` is where chat's permissions are written down.

    A preapproved tool is one whose requests never reach the callback at all,
    which is the only thing a provider allow-list can express. Shell stays off
    it however granted: a shell rule is written per command, and the patterns
    live where the requests arrive.
    """
    granted = EngineConfig(
        approvals=ApprovalConfig(
            allow=(ApprovalCapability.READ, ApprovalCapability.EDIT, ApprovalCapability.BASH)
        )
    )
    argv = build_runners(Settings(engine_config=granted))["claude"].command_line(
        PROFILES[CODER]
    )

    preapproved = argv[argv.index("--allowedTools") + 1 :]
    assert preapproved == ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit"]
    assert "Bash" not in preapproved


def test_the_interactive_codex_sandbox_is_not_narrowed_by_the_policy() -> None:
    """A sandbox is a ceiling, not a preapproval.

    A capability absent from `allow` is one nobody has ruled on, so a person may
    still allow it mid-turn -- and a sandbox narrowed before the turn started
    would refuse the write they just approved. Codex's policy is applied to its
    requests instead.
    """
    reads_only = EngineConfig(approvals=ApprovalConfig(allow=(ApprovalCapability.READ,)))
    argv = build_runners(Settings(engine_config=reads_only))["codex"].command_line(
        PROFILES[CODER]
    )

    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_engine_config_styles_every_claude_runner_this_process_offers() -> None:
    """Chat and review alike: a style is a property of the runner rather than
    of the errand it is sent on."""
    settings = Settings(
        engine_config=EngineConfig(claude=ClaudeConfig(output_style=ResponseStyle.CONCISE))
    )

    for build in (build_runners, build_read_only_runners):
        argv = build(settings)["claude"].command_line(PROFILES[CODER])
        settings_document = json.loads(argv[argv.index("--settings") + 1])
        assert settings_document["outputStyle"] == "Concise"


def test_engine_config_produces_claude_session_config_for_acp_runners() -> None:
    """The same attribution and style settings that reach the CLI runners also
    produce a session config for the ACP graph runners."""
    settings = Settings(
        engine_config=EngineConfig(
            attribution=False,
            claude=ClaudeConfig(output_style=ResponseStyle.CONCISE),
        )
    )
    config = claude_session_config_for(settings)
    assert config is not None
    assert config["claudeCode"]["options"]["settings"]["attribution"]["commit"] == ""
    assert config["claudeCode"]["options"]["settings"]["outputStyle"] == "Concise"


def test_default_engine_config_produces_no_session_config() -> None:
    assert claude_session_config_for(Settings()) is None


def test_a_planning_chat_is_answered_by_the_runner_that_cannot_write(tmp_path) -> None:
    """Half of the Plan button's difference from New chat: the argv.

    Same provider the user picked, same conversation machinery, and a command
    line without the tools to change the checkout it is reading -- a property of
    what the composition hands the planner rather than of its instructions.

    Only half, and the docstring says so deliberately: this proves the planner
    is *handed* less, not that it is *held* to less. A provider asking anyway
    reaches the approval broker, where a policy granting `edit` would allow it;
    what refuses it there is `read_only` on the profile, covered by
    `test_approvals.py`. Either half alone reads like the whole thing, which is
    how a claim like this one comes to be believed without being true.
    """
    settings = Settings(
        engine_config=EngineConfig(
            approvals=ApprovalConfig(
                allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)
            )
        ),
        sqlite_path=str(tmp_path / "conversations.sqlite3"),
    )
    capabilities = build_capabilities(settings)
    try:
        session = build_session(
            capabilities,
            build_runners(settings),
            read_only_runners=build_read_only_runners(settings),
        )
        planner = session.runner_for(PLANNER.agent_id, "claude")
        coder = session.runner_for(CODER, "claude")
    finally:
        capabilities.state_store.close()

    planner_argv = planner.command_line(PLANNER)
    coder_argv = coder.command_line(PROFILES[CODER])

    assert planner_argv[planner_argv.index("--allowedTools") + 1 :] == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert "Edit" in coder_argv[coder_argv.index("--allowedTools") + 1 :]
    codex_argv = session.runner_for(PLANNER.agent_id, "codex").command_line(PLANNER)
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"


def test_milestone_tools_follow_the_project_chat_not_the_selected_agent() -> None:
    class CapturingRunner:
        permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

        def __init__(self) -> None:
            self.mcp_servers: list[McpServerConfig] = []
            self.direct_turns = 0

        async def run_turn(
            self, agent_run_id, profile, messages, tools=(), workspace_id=None
        ):
            self.direct_turns += 1
            return AgentTurn(Message.assistant("ordinary chat"))

        async def run_turn_with_mcp(
            self,
            agent_run_id,
            profile,
            messages,
            mcp_server,
            workspace_id=None,
        ):
            self.mcp_servers.append(mcp_server)
            return AgentTurn(Message.assistant("project chat"))

        async def cancel(self, agent_run_id) -> None:
            pass

    async def scenario() -> tuple[CapturingRunner, AgentProfile]:
        store = InMemoryStateStore()
        runner = CapturingRunner()
        session = build_session(
            Capabilities(
                workflow_runtime=None,
                source_control=None,
                agent_runner=runner,
                communications=None,
                workspace_provider=ConversationWorkspaces(),
                state_store=store,
            ),
            {"test": runner},
        )
        project_chat = await session.start(CODER, runner="test")
        await store.save_project(
            Project(project_id_for_instance(project_chat.instance_id), "OpenEngine")
        )
        await session.say(project_chat.instance_id, "Plan this.", runner="test")

        ordinary_planner = await session.start(PLANNER.agent_id, runner="test")
        await session.say(ordinary_planner.instance_id, "Plan this.", runner="test")
        return runner, session.profiles[PLANNER.agent_id]

    runner, planner_profile = asyncio.run(scenario())
    config = runner.mcp_servers[0]
    advertised = tuple(
        config.args[index + 1]
        for index, argument in enumerate(config.args)
        if argument == "--capability"
    )

    assert advertised == (
        "add_milestone",
        "list_milestones",
        "update_milestone",
        "delete_milestone",
        "add_workstream",
        "update_workstream",
        "delete_workstream",
    )
    assert runner.direct_turns == 1
    assert planner_profile.capabilities == ()


def test_review_comments_reach_the_github_api(tmp_path) -> None:
    """Comments are posted via the GitHub API, not via the gh CLI.

    Proved by intercepting the HTTP request rather than by reading constructor
    arguments back: what matters is that the adapter actually calls the right
    endpoint with the right payload.
    """
    recorded: list[httpx.Request] = []

    async def fake_api(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict:
        recorded.append(httpx.Request(method, f"https://api.github.com{path}"))
        return {"id": 123, "html_url": "https://github.com/acme/api/pull/7#issuecomment-123"}

    from engine.adapters.source_control.github import GitHubSourceControl
    from unittest.mock import patch

    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        with patch.object(GitHubSourceControl, "_api", fake_api):
            asyncio.run(
                capabilities.source_control.add_comment(
                    "https://github.com/acme/api/pull/7", "Looks right."
                )
            )
    finally:
        capabilities.state_store.close()

    assert len(recorded) == 1
    assert recorded[0].method == "POST"
    assert "/repos/acme/api/issues/7/comments" in str(recorded[0].url)


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

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

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


def _session(runner: ConcurrentRunner) -> AgentSession:
    return _session_with({"test": runner})


def _session_with(
    runners: Mapping[str, ConcurrentRunner],
    profiles: Mapping[AgentId, AgentProfile] = PROFILES,
    state_store: InMemoryStateStore | None = None,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=next(iter(runners.values())),
            communications=unused,
            workspace_provider=unused,
            state_store=state_store or InMemoryStateStore(),
        ),
        profiles=profiles,
        runners=dict(runners),
    )


def _workflow_app(
    store: InMemoryStateStore,
    runner: ConcurrentRunner,
    workspaces: object | None = None,
    communications: object | None = None,
    runners: dict[str, ConcurrentRunner] | None = None,
    workflow_catalog: WorkflowCatalog | None = None,
    workspace_repository: str | None = None,
    graph_runtime=None,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    public_url: str = "",
    utilization: UtilizationService | None = None,
):
    """Wire the app the way the composition root does."""
    unused = object()
    chat_runners: dict[str, ConcurrentRunner] = dict(runners or {"test": runner})
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=communications if communications is not None else unused,
            workspace_provider=workspaces or ConversationWorkspaces(),
            state_store=store,
        ),
        profiles=PROFILES,
        runners=chat_runners,
        workspace_repository=workspace_repository,
    )
    return create_app(
        session,
        chat_runners,
        workflow_catalog=(
            workflow_catalog
            if workflow_catalog is not None
            else WorkflowCatalog.from_graphs(())
        ),
        graph_runtime=graph_runtime,
        approval_policy=approval_policy,
        public_url=public_url,
        utilization=utilization,
    )


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


def _work_order(
    phase: RunPhase = RunPhase.RUNNING_AGENT, *, failure_reason: str = ""
) -> RunState:
    """The row a graph WorkOrder is listed and opened by."""
    return RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-rerank"),
        phase=phase,
        repository="acme/api",
        prompt="Add cancellation handling.",
        name="Add cancellation handling",
        failure_reason=failure_reason,
    )


def test_deleting_a_run_forgets_it() -> None:
    """The rail's × on a WorkOrder is not the project row's archive.

    Nothing lists or restores what it removes, so the row goes for good.
    """
    store = InMemoryStateStore()
    state = _work_order(RunPhase.SUCCEEDED)
    asyncio.run(store.save(state))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            deleted = await client.delete(f"/api/runs/{state.run_id}")
            listed = await client.get("/api/runs")
            detail = await client.get(f"/api/runs/{state.run_id}")
            again = await client.delete(f"/api/runs/{state.run_id}")
            return deleted, listed, detail, again

    deleted, listed, detail, again = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert listed.json()["runs"] == []
    assert detail.status_code == 404
    # A second × on a row the poll has not cleared yet says the same thing the
    # page does, rather than pretending to delete it twice.
    assert again.status_code == 404
    assert asyncio.run(store.load(state.run_id)) is None


def test_utilization_is_served_from_the_cache_and_then_scraped(tmp_path) -> None:
    """The two calls the page makes, and why there are two of them.

    Opening it must draw something before either provider answers, so the cache
    is a read of its own that touches no network; the scrape that follows is
    what replaces the figures with today's.
    """
    reading = RunnerUtilization(
        runner="claude",
        plan="max",
        windows=(
            UtilizationWindow("five_hour", "5-hour", 12.0, "2026-09-08T20:10:00+00:00"),
            UtilizationWindow("seven_day", "Weekly", 41.0, "2026-09-10T02:00:00+00:00"),
        ),
    )

    async def read_claude(_client) -> RunnerUtilization:
        return reading

    utilization = UtilizationService(
        cache_path=tmp_path / "utilization.json", readers={"claude": read_claude}
    )
    app = _workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        runners={"claude": ConcurrentRunner(), "codex": ConcurrentRunner()},
        utilization=utilization,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            empty = await client.get("/api/utilization")
            scraped = await client.post("/api/utilization/refresh")
            cached = await client.get("/api/utilization")
            return empty, scraped, cached

    empty, scraped, cached = asyncio.run(scenario())

    # Nothing has been read yet, which is a page with no meters rather than an
    # error: the scrape is what fills it.
    assert empty.status_code == 200
    assert empty.json() == {"runners": []}
    assert scraped.status_code == 200
    listed = scraped.json()["runners"]
    # Only the runner something knows how to read, even though the deployment
    # offers two.
    assert [entry["runner"] for entry in listed] == ["claude"]
    assert listed[0]["plan"] == "max"
    assert [window["label"] for window in listed[0]["windows"]] == ["5-hour", "Weekly"]
    assert [window["usedPercent"] for window in listed[0]["windows"]] == [12.0, 41.0]
    # And the next open starts from what the scrape found, without asking again.
    assert cached.json() == scraped.json()


def test_utilization_refresh_refuses_a_cross_origin_page(tmp_path) -> None:
    """It reads the tokens the runners signed in with, so it is guarded like
    every other endpoint that touches a stored credential."""
    asked = False

    async def read_claude(_client) -> RunnerUtilization:
        nonlocal asked
        asked = True
        return RunnerUtilization(runner="claude")

    app = _workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        runners={"claude": ConcurrentRunner()},
        utilization=UtilizationService(
            cache_path=tmp_path / "utilization.json", readers={"claude": read_claude}
        ),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/utilization/refresh", headers={"origin": "https://elsewhere.example"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 403
    assert not asked


def test_run_list_leaves_the_prose_to_the_run_it_names() -> None:
    """Every screen polls `/api/runs` once a second to keep its rail current.

    What that list carries is what every screen pays for, on a payload that
    grows with every run ever started -- so the words an agent wrote stay with
    the single run the page showing them asks for, and the list keeps what a
    rail, a card and a milestone's task list read.
    """
    store = InMemoryStateStore()
    state = _work_order(RunPhase.FAILED, failure_reason="the reviewer gave up")
    asyncio.run(store.save(state))
    app = _workflow_app(store, ConcurrentRunner())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                await client.get("/api/runs"),
                await client.get(f"/api/runs/{state.run_id}"),
            )

    listed, detail = asyncio.run(scenario())
    (run,) = listed.json()["runs"]
    body = detail.json()

    prose = {"taskPrompt", "failureReason"}
    assert prose.isdisjoint(run)
    assert prose <= set(body)
    # What the rail and the WorkOrder cards do read, which is how far the
    # listing can be trimmed before a screen loses something it draws.
    assert run == {
        key: value for key, value in body.items() if key not in prose
    }
    assert body["failureReason"] == "the reviewer gave up"

def test_approval_feed_replays_and_pushes_broker_transitions() -> None:
    store = InMemoryStateStore()
    feed = ApprovalFeed(store)
    broker = ApprovalBroker(store, observe=feed.publish)

    async def scenario():
        instance = await store.create_instance(CODER)
        stream = feed.stream(instance.instance_id)
        assert await anext(stream) == b": connected\n\n"

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-feed"),
            instance_id=instance.instance_id,
            runner="test",
            present=lambda _approval: asyncio.sleep(0),
        )
        waiting = asyncio.create_task(
            handler(
                ApprovalRequest(
                    approval_id="provider-approval",
                    kind=ApprovalKind.COMMAND_EXECUTION,
                    reason="Run the test suite",
                    command="pytest",
                    cwd="/workspace",
                )
            )
        )
        pending = await asyncio.wait_for(anext(stream), timeout=1)
        record = (await store.list_approvals())[0]
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-feed"),
        )
        decided = await asyncio.wait_for(anext(stream), timeout=1)
        await waiting
        await stream.aclose()
        return [
            json.loads(frame.decode().removeprefix("data:"))
            for frame in (pending, decided)
        ]

    events = asyncio.run(scenario())

    assert [event["status"] for event in events] == ["pending", "decided"]
    assert events[0]["id"] == events[1]["id"]
    assert events[1]["decision"] == "accept"


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
        {
            "workflowId": "implementation-review-v1",
            "prompt": "Task",
            "repository": ".",
            "workstreamId": "unknown",
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


def test_create_workflow_run_uses_workstream_or_milestone_relationship() -> None:
    store = InMemoryStateStore()
    project = Project(ProjectId("project-engine"), "Engine")
    milestone = Milestone(
        MilestoneId("milestone-foundation"), project.project_id, "Foundation"
    )
    workstream = Workstream(
        WorkstreamId("workstream-data"), milestone.milestone_id, "Data model"
    )
    asyncio.run(store.save_project(project))
    asyncio.run(store.save_milestone(milestone))
    asyncio.run(store.save_workstream(workstream))
    app, _runtime = _graph_app(store, _review_graph())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            async with app.router.lifespan_context(app):
                direct = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Document the milestone.",
                        "repository": ".",
                        "milestoneId": milestone.milestone_id,
                    },
                )
                scoped = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Persist the model.",
                        "repository": ".",
                        "milestoneId": milestone.milestone_id,
                        "workstreamId": workstream.workstream_id,
                    },
                )
                return direct, scoped

    direct, scoped = asyncio.run(scenario())

    assert (direct.status_code, scoped.status_code) == (201, 201)
    assert (direct.json()["milestoneId"], direct.json()["workstreamId"]) == (
        milestone.milestone_id,
        None,
    )
    assert (scoped.json()["milestoneId"], scoped.json()["workstreamId"]) == (
        None,
        workstream.workstream_id,
    )


#: The dev server's proxy table. TypeScript because Vite is what reads it, so
#: this is the one list about this application that cannot be imported.
PROXY_SOURCE = Path(__file__).resolve().parent.parent / "apps/web/src/api-proxy.ts"


def _proxied_prefixes() -> set[str]:
    """`PROXIED_PREFIXES`, read out of the source rather than restated here.

    Read the way `layout.py` reads `capabilities.py`: a second copy of a list
    that must not drift is the thing that drifts.
    """
    source = PROXY_SOURCE.read_text()
    listing = re.search(r"PROXIED_PREFIXES\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert listing is not None, f"no PROXIED_PREFIXES in {PROXY_SOURCE}"
    return set(re.findall(r'"([^"]+)"', listing.group(1)))


def test_every_prefix_this_application_serves_is_one_the_dev_server_forwards() -> None:
    """The failure this is here for is silent, and only in development.

    `apps/web/vite.config.ts` forwards the prefixes it was told about and
    answers everything else with `index.html` and a 200, so a prefix this
    application serves and the proxy has not heard of does not arrive as a 404
    -- the client gets a page where it asked for JSON, and reports a parse
    error. Every other test in this file talks to the application directly and
    cannot see it. That is how `/graph` was served, read by the client, and
    unproxied for two releases.

    Composed without a static directory, so what is left is the surface that is
    not the client's own: the SPA's pages are Vite's to answer and must not be
    forwarded.
    """
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()})

    served = {
        "/" + route.path.lstrip("/").split("/")[0]
        for route in app.routes
        # The placeholder page for a checkout with no build, which is the
        # client's address rather than this application's.
        if route.path != "/"
    }

    # Containment rather than equality in both directions: adding a prefix to
    # both sides is the correct change and must stay green, and a test that
    # went red for it would be edited into agreement without being read.
    assert {"/api", "/graph"} <= served
    assert served <= _proxied_prefixes()


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


def test_milestone_frontend_routes_serve_the_application(tmp_path) -> None:
    """A plan's pages are reached by URL as well as by click.

    Both are deep links the client routes itself: the plan, and one goal off it
    opened from a workstream on the timeline. Without a route apiece, a refresh
    or a pasted link falls through to the static mount and 404s.
    """
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = create_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                await client.get("/projects/project-42/milestones"),
                await client.get("/projects/project-42/milestones/milestone-7"),
            )

    plan, milestone = asyncio.run(scenario())

    assert plan.status_code == 200
    assert "workflow application" in plan.text
    assert milestone.status_code == 200
    assert "workflow application" in milestone.text


class ConversationWorkspaces:
    """A provider whose checkouts come and go, as real ones do."""

    def __init__(self) -> None:
        self.count = 0
        self.detached: set[str] = set()
        self.attachments: list[tuple[str, str, str]] = []

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
        self.attachments.append((workspace_id, repository, base_ref))
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


def test_the_config_names_the_agent_the_plan_button_talks_to() -> None:
    """The client asks which agent plans rather than knowing an id of its own,
    and is told nothing when a composition has no planner to offer."""
    runner = ConcurrentRunner()
    shipped = create_app(_session_with({"test": runner}, BUILT_IN), {"test": runner})
    coders_only = create_app(_session(runner), {"test": runner})

    async def config(app) -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get("/api/config")).json()

    shipped_config, narrow_config = asyncio.run(config(shipped)), asyncio.run(config(coders_only))

    assert shipped_config["planAgent"] == "planner"
    assert "planner" in [agent["id"] for agent in shipped_config["agents"]]
    assert shipped_config["defaultAgent"] == "coder"
    assert narrow_config["planAgent"] == ""


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


def test_projects_api_creates_and_lists_projects_newest_first() -> None:
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            missing = await client.post("/api/projects", json={})
            first = await client.post(
                "/api/projects", json={"name": "First project"}
            )
            second = await client.post(
                "/api/projects", json={"name": "Second project"}
            )
            listed = await client.get("/api/projects")
            return missing, first, second, listed

    missing, first, second, listed = asyncio.run(scenario())

    assert missing.status_code == 400
    assert first.status_code == 201
    assert first.json()["projectId"].startswith("project-")
    assert first.json()["name"] == "First project"
    assert second.status_code == 201
    assert [project["name"] for project in listed.json()["projects"]] == [
        "Second project",
        "First project",
    ]
    # Recorded directly rather than by planning, so there is no conversation to
    # open and the rail has nowhere to send a click.
    assert all(
        "conversationUrl" not in project
        for project in listed.json()["projects"]
    )


def test_a_project_is_archived_and_restored_the_way_a_chat_is() -> None:
    """Archiving puts a project away rather than deleting it: it stays listed,
    marked so the rail can file it under its own heading, and restoring is the
    same click back. The plan it was named after is untouched by either."""

    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test", "createProject": True},
            )
            project_id = f"project-{created.json()['id']}"
            archived = await client.post(f"/api/projects/{project_id}/archive")
            listed = await client.get("/api/projects")
            restored = await client.post(f"/api/projects/{project_id}/unarchive")
            missing = await client.post("/api/projects/project-missing/archive")
            return created, archived, listed, restored, missing

    created, archived, listed, restored, missing = asyncio.run(scenario())

    thread_id = created.json()["id"]
    assert archived.status_code == 200
    assert archived.json() == {
        "projectId": f"project-{thread_id}",
        "name": "New project",
        "archived": True,
        "milestoneCount": 0,
        # The plan is still open, and restoring has to give the link back.
        "conversationUrl": f"/conversations/{thread_id}",
    }
    assert listed.json()["projects"] == [archived.json()]
    assert restored.json()["archived"] is False
    assert missing.status_code == 404


def test_project_milestones_api_lists_the_active_projects_dependency_data() -> None:
    runner = ConcurrentRunner()
    session = _session(runner)
    project = Project(project_id_for_instance(AgentInstanceId("agi-plan")), "Engine")
    foundation = Milestone(
        MilestoneId("milestone-foundation"),
        project.project_id,
        "Foundation",
        "Build the shared planning model.",
    )
    launch = Milestone(
        MilestoneId("milestone-launch"),
        project.project_id,
        "Launch",
        "Put the project in users' hands.",
        (foundation.milestone_id,),
    )
    data_model = Workstream(
        WorkstreamId("workstream-data"),
        foundation.milestone_id,
        "Data model",
        "The store, its ports, and its migrations.",
    )

    async def scenario():
        await session.state_store.save_project(project)
        await session.state_store.save_milestone(foundation)
        await session.state_store.save_milestone(launch)
        await session.state_store.save_workstream(data_model)
        app = create_app(session, {"test": runner})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            listed = await client.get(
                f"/api/projects/{project.project_id}/milestones"
            )
            missing = await client.get("/api/projects/project-missing/milestones")
            return listed, missing

    listed, missing = asyncio.run(scenario())

    assert listed.json() == {
        "project": {
            "projectId": project.project_id,
            "name": "Engine",
            "archived": False,
        },
        "milestones": [
            {
                "milestoneId": "milestone-launch",
                "name": "Launch",
                "description": "Put the project in users' hands.",
                "dependencies": ["milestone-foundation"],
                "workstreams": [],
            },
            {
                "milestoneId": "milestone-foundation",
                "name": "Foundation",
                "description": "Build the shared planning model.",
                "dependencies": [],
                "workstreams": [
                    {
                        "workstreamId": "workstream-data",
                        "name": "Data model",
                        "scope": "The store, its ports, and its migrations.",
                    }
                ],
            },
        ],
    }
    assert missing.status_code == 404


def test_project_milestones_api_links_the_project_back_to_its_plan() -> None:
    """The milestones page is reached from the rail rather than from the plan,
    so the way back to the conversation has to come with the answer."""

    runner = ConcurrentRunner()
    session = _session(runner)
    app = create_app(session, {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test", "createProject": True},
            )
            thread_id = created.json()["id"]
            project_id = ProjectId(f"project-{thread_id}")
            await session.state_store.save_milestone(
                Milestone(MilestoneId("milestone-1"), project_id, "Foundation")
            )
            listed = await client.get(f"/api/projects/{project_id}/milestones")
            return thread_id, listed

    thread_id, listed = asyncio.run(scenario())

    assert listed.json()["project"]["conversationUrl"] == f"/conversations/{thread_id}"
    assert [milestone["name"] for milestone in listed.json()["milestones"]] == [
        "Foundation"
    ]


def test_milestone_scope_api_invokes_scoper_with_milestone_context_and_current_work() -> None:
    class RecordingMilestoneScoper:
        request = None

        async def run(self, **request):
            self.request = request
            milestone_id = request["milestone"].milestone_id
            return ScopingPlan(
                create=(
                    WorkOrderSpec(
                        milestone_id,
                        "Render the plan",
                        "Draw the proposed work orders.",
                    ),
                ),
                cancel=(WorkOrderId("run-obsolete"),),
                reasons=("The milestone needs a dedicated scoping view.",),
            )

    store = InMemoryStateStore()
    session = _session_with({"test": ConcurrentRunner()}, state_store=store)
    scoper = RecordingMilestoneScoper()
    project = Project(ProjectId("project-engine"), "Engine")
    milestone = Milestone(
        MilestoneId("milestone-scoping"),
        project.project_id,
        "Milestone scoping",
        "Break milestone requirements into reviewable work orders.",
    )
    existing = RunState(
        run_id=RunId("run-existing"),
        task_id=TaskId("task-existing"),
        workflow_id=WorkflowId("implementation-review-rerank"),
        milestone_id=milestone.milestone_id,
        phase=RunPhase.RUNNING_AGENT,
        name="Existing implementation",
        prompt="Implement the existing portion.",
    )

    async def scenario():
        await store.save_project(project)
        await store.save_milestone(milestone)
        await store.save(existing)
        app = create_app(
            session,
            {"test": ConcurrentRunner()},
            milestone_scoper=scoper,  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.post(
                f"/api/projects/{project.project_id}/milestones/"
                f"{milestone.milestone_id}/scope",
                json={"message": "Prefer changes under 1,000 lines."},
            )

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {
        "create": [
            {
                "milestoneId": "milestone-scoping",
                "name": "Render the plan",
                "objective": "Draw the proposed work orders.",
                "evidenceRequirements": [],
                "dependencies": [],
            }
        ],
        "cancel": ["run-obsolete"],
        "supersede": [],
        "reasons": ["The milestone needs a dedicated scoping view."],
    }
    scheduled = [run for run in asyncio.run(store.list_runs()) if run.run_id != existing.run_id]
    assert len(scheduled) == 1
    assert scheduled[0].phase is RunPhase.SCHEDULED
    assert scheduled[0].name == "Render the plan"
    assert scheduled[0].milestone_id == milestone.milestone_id
    assert scoper.request["milestone"].name == "Milestone scoping"
    assert scoper.request["milestone"].requirements == (
        "Break milestone requirements into reviewable work orders.",
    )
    assert scoper.request["policy"].rules == (
        "Prefer changes under 1,000 lines.",
    )
    assert scoper.request["workorders"][0].status is WorkOrderStatus.IN_PROGRESS
    assert scoper.request["workorders"][0].spec.objective == (
        "Implement the existing portion."
    )


def test_projects_api_says_how_many_milestones_each_project_has() -> None:
    """The rail offers a project's plan only where there is one to offer.

    Counted by the store rather than in the handler: the shell polls this route
    every second, so neither a query per project nor a read of every milestone
    row will do -- one grows with the list, the other with the total size of
    every plan in the store. Reading a milestone at all is the failure, which is
    why the double refuses rather than counts.
    """

    class ForbidsMilestoneReads(InMemoryStateStore):
        async def list_milestones(self, project_id=None):
            raise AssertionError("counting must not hydrate milestone rows")

    runner = ConcurrentRunner()
    store = ForbidsMilestoneReads()
    session = _session_with({"test": runner}, state_store=store)
    planned = Project(ProjectId("project-planned"), "Engine roadmap")
    empty = Project(ProjectId("project-empty"), "Nothing planned yet")

    async def scenario():
        await store.save_project(planned)
        await store.save_project(empty)
        for index in range(3):
            await store.save_milestone(
                Milestone(
                    MilestoneId(f"milestone-{index}"),
                    planned.project_id,
                    f"Goal {index}",
                )
            )
        app = create_app(session, {"test": runner})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get("/api/projects")

    listed = asyncio.run(scenario())

    assert {
        project["name"]: project["milestoneCount"]
        for project in listed.json()["projects"]
    } == {"Engine roadmap": 3, "Nothing planned yet": 0}


def test_archiving_a_project_answers_with_the_plan_it_keeps() -> None:
    """Archiving is not deleting, and the answer has to say so.

    The route sends the whole row the list would, so a client that redraws from
    it is not left with a project missing half itself -- and restoring gives the
    milestones back rather than reporting a plan of none.
    """

    runner = ConcurrentRunner()
    store = InMemoryStateStore()
    session = _session_with({"test": runner}, state_store=store)
    app = create_app(session, {"test": runner})
    project = Project(ProjectId("project-planned"), "Engine roadmap")

    async def scenario():
        await store.save_project(project)
        for index in range(2):
            await store.save_milestone(
                Milestone(
                    MilestoneId(f"milestone-{index}"),
                    project.project_id,
                    f"Goal {index}",
                )
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            archived = await client.post("/api/projects/project-planned/archive")
            restored = await client.post("/api/projects/project-planned/unarchive")
            return archived, restored

    archived, restored = asyncio.run(scenario())

    assert archived.json() == {
        "projectId": "project-planned",
        "name": "Engine roadmap",
        "archived": True,
        "milestoneCount": 2,
    }
    assert restored.json() == {**archived.json(), "archived": False}


def test_project_milestones_api_costs_the_same_reads_however_long_the_plan_is() -> None:
    """The timeline polls this route every second, per open project.

    A read per milestone would make each poll cost the length of the plan, and
    the SQLite store serializes every query behind one connection, so the plan
    is read whole and grouped in the handler instead.
    """

    class CountingStore(InMemoryStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.workstream_reads = 0

        async def list_workstreams(self, milestone_id=None):
            self.workstream_reads += 1
            return await super().list_workstreams(milestone_id)

    runner = ConcurrentRunner()
    store = CountingStore()
    session = _session_with({"test": runner}, state_store=store)
    project = Project(project_id_for_instance(AgentInstanceId("agi-long")), "Engine")

    async def scenario():
        await store.save_project(project)
        for index in range(12):
            milestone = Milestone(
                MilestoneId(f"milestone-{index}"), project.project_id, f"Goal {index}"
            )
            await store.save_milestone(milestone)
            await store.save_workstream(
                Workstream(
                    WorkstreamId(f"workstream-{index}"),
                    milestone.milestone_id,
                    f"Work {index}",
                    "One workstream per goal.",
                )
            )
        store.workstream_reads = 0
        app = create_app(session, {"test": runner})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get(f"/api/projects/{project.project_id}/milestones")

    listed = asyncio.run(scenario())

    assert store.workstream_reads == 1
    milestones = listed.json()["milestones"]
    assert len(milestones) == 12
    assert [milestone["workstreams"][0]["name"] for milestone in milestones] == [
        f"Work {index}" for index in reversed(range(12))
    ]


def test_new_project_intent_is_durable_before_the_agent_names_it() -> None:
    runner = ConcurrentRunner(('"Durable project intent"',))
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/threads",
                json={
                    "agentId": "coder",
                    "runner": "test",
                    "createProject": True,
                },
            )
            before_title = await client.get("/api/projects")
            titled = await client.post(
                f"/api/threads/{created.json()['id']}/title",
                json={"text": "Keep this intent across a reload"},
            )
            after_title = await client.get("/api/projects")
            return created, before_title, titled, after_title

    created, before_title, titled, after_title = asyncio.run(scenario())

    assert created.status_code == 201
    assert created.json()["title"] == "New project"
    assert before_title.json()["projects"] == [
        {
            "projectId": f"project-{created.json()['id']}",
            "name": "New project",
            "archived": False,
            "milestoneCount": 0,
            "conversationUrl": f"/conversations/{created.json()['id']}",
        }
    ]
    assert titled.json() == {"title": "Durable project intent"}
    assert after_title.json()["projects"] == [
        {
            "projectId": f"project-{created.json()['id']}",
            "name": "Durable project intent",
            "archived": False,
            "milestoneCount": 0,
            "conversationUrl": f"/conversations/{created.json()['id']}",
        }
    ]


def test_an_archived_plan_leaves_its_project_with_nowhere_to_go() -> None:
    """Archiving is one click away in the rail, and the archived conversation
    opens as a blank new chat. The project is still listed -- it exists -- but
    without a link, which is the row a project with no conversation already
    gets. Restoring the chat gives the link back."""

    runner = ConcurrentRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test", "createProject": True},
            )
            thread_id = created.json()["id"]
            await client.post(f"/api/threads/{thread_id}/archive")
            archived = await client.get("/api/projects")
            await client.post(f"/api/threads/{thread_id}/unarchive")
            restored = await client.get("/api/projects")
            return thread_id, archived, restored

    thread_id, archived, restored = asyncio.run(scenario())

    assert archived.json()["projects"] == [
        {
            "projectId": f"project-{thread_id}",
            "name": "New project",
            "archived": False,
            "milestoneCount": 0,
        }
    ]
    assert restored.json()["projects"] == [
        {
            "projectId": f"project-{thread_id}",
            "name": "New project",
            "archived": False,
            "milestoneCount": 0,
            "conversationUrl": f"/conversations/{thread_id}",
        }
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


def test_replayed_tool_call_id_is_only_exposed_once() -> None:
    """Provider reconnects may repeat a completed item with its original id.

    assistant-ui treats the id as a resource key across the whole thread, so a
    replay must remain one displayed call rather than crashing the chat view.
    """
    call = ToolCall(
        call_id="call-replayed",
        name="Read",
        arguments='{"path":"README.md"}',
    )

    class ReplayRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ReplayRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "inspect"}
            )
            replayed = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "inspect again"}
            )
            history = await client.get(f"/api/threads/{thread_id}/messages")
            return replayed, history

    replayed, history = asyncio.run(scenario())

    assert "call-replayed" not in replayed.text
    parts = [
        part
        for message in history.json()["messages"]
        for part in message["content"]
        if part["type"] == "tool-call"
    ]
    assert [part["toolCallId"] for part in parts] == ["call-replayed"]
    assert parts[0]["result"] == "engine"


def test_a_stopped_run_leaves_its_work_in_the_reloaded_transcript() -> None:
    """Pressing stop ends the turn, not the record of it. What the agent had
    already done is on disk whatever the button does, so a reload that showed
    the question alone would be a transcript the worktree disagrees with."""
    call = ToolCall(call_id="call-1", name="Write", arguments='{"path":"worker.py"}')

    class StoppedMidWorkRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.reported = asyncio.Event()

        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            raise AssertionError("the streaming method should be used")

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            on_message(Message.assistant("Rewriting the worker."))
            on_message(Message.assistant(tool_calls=(call,)))
            self.reported.set()
            await asyncio.Event().wait()
            raise AssertionError("this runner only ever ends by being stopped")

    runner = StoppedMidWorkRunner()
    app = create_app(_session(runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "rewrite it"})
            )
            await runner.reported.wait()
            stopped = await client.delete(f"/api/threads/{thread_id}/runs/current")
            await started
            # A fresh page load: the stream is gone, so this is all the client
            # gets to know about the turn that was stopped.
            return stopped, await client.get(f"/api/threads/{thread_id}/messages")

    stopped, messages = asyncio.run(scenario())

    assert stopped.status_code == 204
    reloaded = messages.json()["messages"]
    # The note the next turn is given is prompt context, not something to show
    # a person, so it does not become a message here.
    assert [message["role"] for message in reloaded] == ["user", "assistant"]
    assert reloaded[1]["content"] == [
        {"type": "text", "text": "Rewriting the worker."},
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Write",
            "args": {"path": "worker.py"},
            "argsText": '{"path":"worker.py"}',
            # Answered rather than left pending, which is what a client shows
            # as a tool still running.
            "result": "interrupted",
        },
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


# --- graph WorkOrders (the graph entries in the dropdown) ---------------------
#
# A second kind of workflow can be picked from the same dropdown. It is run by
# the graph engine rather than by the step executor, and these are the three
# things that has to mean: it is offered, picking it starts a graph run, and
# what this app keeps for it is a row rather than a driver.


def _graph_app(
    store: InMemoryStateStore,
    *graphs: ScriptedGraph,
    approval_policy: ApprovalConfig = ApprovalConfig(),
):
    """The web app with a scripted graph engine wired in.

    A real `GraphRuntime` with real tasks, exactly as the graph package's own
    tests use it -- what it does not have is LangGraph, so no agent is started
    and no repository is checked out.
    """
    runtime = ScriptedGraphRuntime(*graphs)
    return (
        _graph_app_over(store, runtime, *graphs, approval_policy=approval_policy),
        runtime,
    )


def _graph_app_over(
    store: InMemoryStateStore,
    runtime: ScriptedGraphRuntime,
    *graphs: ScriptedGraph,
    approval_policy: ApprovalConfig = ApprovalConfig(),
):
    """A web app over an engine that already exists, so a restart can be one.

    The lifespan is what picks graph WorkOrders back up, and a context manager
    is entered once -- so "the server was restarted" is a second app over the
    same store and the same engine, rather than the same app opened twice.
    """

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    return _workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs(graphs),
        graph_runtime=running(),
        approval_policy=approval_policy,
    )


def _review_graph() -> ScriptedGraph:
    return ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Changed it."),)),),
    )


def test_a_workflow_is_offered_under_its_own_name() -> None:
    """The dropdown, which is where a person meets this at all.

    Asked of a started server, because that is when a workflow is offerable:
    the engine that would run one is opened on startup.
    """
    app, _ = _graph_app(InMemoryStateStore(), _review_graph())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return (await client.get("/api/config")).json()["workflows"]

    offered = asyncio.run(scenario())

    assert offered == [
        {
            "id": "implementation-review-codex",
            "name": "Implementation review (codex)",
        },
    ]


def test_a_workflow_is_not_offered_without_an_engine_to_run_it() -> None:
    """No graph engine composed, nothing on offer.

    The alternative is a choice that fails after somebody made it, which is
    worse than a choice that was never there.
    """
    app = _workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return (
                (await client.get("/api/config")).json()["workflows"],
                await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                ),
            )

    offered, refused = asyncio.run(scenario())

    assert offered == []
    assert refused.status_code == 400


def test_creating_a_work_order_starts_the_graph() -> None:
    """The whole point: picking one runs it on the graph engine.

    Checked on the engine rather than only on the answer, because a WorkOrder
    that was recorded and never started would look identical from here.
    """
    store = InMemoryStateStore()
    app, runtime = _graph_app(store, _review_graph())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                        "runner": "test",
                    },
                )
                run_id = RunId(created.json()["runId"])
                listed = await client.get("/api/runs")
                return created, listed, run_id, await runtime.snapshot(run_id)

    created, listed, run_id, snapshot = asyncio.run(scenario())

    assert created.status_code == 201
    # One run, on the graph engine, carrying the task and the repository the
    # WorkOrder was created with -- which is everything these graphs need.
    assert snapshot is not None
    assert str(snapshot.graph_id) == "implementation-review-codex"
    assert snapshot.values == {
        "task": "Add cancellation handling.",
        "repository": "acme/api",
    }
    # And a row for it here, under the graph engine's own run id, named after
    # the graph rather than after an id nobody chose.
    assert created.json()["workflowName"] == "Implementation review (codex)"
    assert [one["runId"] for one in listed.json()["runs"]] == [str(run_id)]


def test_graph_run_listing_carries_live_node_and_approval_state() -> None:
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Ask("Run tests"),)),),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": str(graph.graph_id),
                        "prompt": "Implement it",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                for _ in range(100):
                    snapshot = await runtime.snapshot(run_id)
                    if snapshot is not None and snapshot.pending_approvals:
                        break
                    await asyncio.sleep(0)
                else:
                    raise AssertionError("graph never requested approval")
                return (await client.get("/api/runs")).json()["runs"][0]

    row = asyncio.run(scenario())
    assert row["graphProgress"] == {
        "activeNodeIds": ["implementation"],
        "waitingNodeIds": ["implementation"],
        "nextNodeIds": [],
    }


def test_auto_approve_config_seeds_all_graph_nodes() -> None:
    """When `auto_approve = true`, every node starts auto-approved."""
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (
            ScriptedNode(
                NodeId("implementation"),
                (Say("Changed it."),),
                next_nodes=(NodeId("review"),),
            ),
            ScriptedNode(NodeId("review"), (Say("Looks good."),)),
        ),
    )
    app, runtime = _graph_app(
        InMemoryStateStore(),
        graph,
        approval_policy=ApprovalConfig(auto_approve=True),
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": str(graph.graph_id),
                        "prompt": "Review it",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                snapshot = await runtime.snapshot(run_id)
                return snapshot

    snapshot = asyncio.run(scenario())
    assert set(snapshot.auto_approve_nodes) == {
        NodeId("implementation"),
        NodeId("review"),
    }


def test_a_graph_naming_node_names_its_work_order() -> None:
    store = InMemoryStateStore()
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (
            ScriptedNode(
                NodeId("naming"),
                (Say('"Cancellation handling."'),),
                next_nodes=(NodeId("implementation"),),
                output_key="name",
            ),
            ScriptedNode(NodeId("implementation"), (AwaitSteering(),)),
        ),
    )
    app, _ = _graph_app(store, graph)

    async def scenario() -> dict[str, object]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = created.json()["runId"]
                for _ in range(100):
                    named = (await client.get(f"/api/runs/{run_id}")).json()
                    if named["name"] == "Cancellation handling":
                        return named
                    await asyncio.sleep(0)
                return named

    named = asyncio.run(scenario())

    assert named["name"] == "Cancellation handling"


def test_a_finished_graph_run_stops_saying_it_is_working() -> None:
    """The row follows the graph to its ending.

    Nothing else would move it: the step executor is not driving this run, so
    without the engine's own report the WorkOrder would claim to be working
    forever.
    """
    store = InMemoryStateStore()
    app, _ = _graph_app(store, _review_graph())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                return created.json()["phase"], (
                    await _await_phase(client, run_id, "succeeded")
                ).json()["phase"]

    started, ended = asyncio.run(scenario())

    assert started == "running_agent"
    assert ended == "succeeded"


def test_deleting_a_graph_work_order_stops_the_engine_driving_it() -> None:
    """The rail's x on a graph row has to reach the other engine.

    None of what stops a step WorkOrder touches a graph one: its driver is a
    task inside the graph engine rather than in this app's `workflow_tasks`,
    and the agent it has open is not an agent run this app started. So a delete
    that only forgot the row would take the WorkOrder off the rail and leave
    the run working -- agents still going in the repository, and nothing left
    on screen to stop them by.

    Scripted on a node that waits, so there is something still in flight at the
    moment the row is deleted; a graph that had already finished would pass
    this whatever the handler did.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                # The node is scripted to wait, so this is a run with something
                # genuinely in flight rather than one that raced to its end.
                while not runtime.running():
                    await asyncio.sleep(0)
                deleted = await client.delete(f"/api/runs/{run_id}")
                listed = await client.get("/api/runs")
                # Read here rather than after the loop is closed, which would
                # cancel the driver itself and pass whether or not the delete
                # had.
                driving = [str(one) for one in runtime.running()]
                return deleted, listed, run_id, driving, await runtime.snapshot(run_id)

    deleted, listed, run_id, driving, snapshot = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert listed.json()["runs"] == []
    assert asyncio.run(store.load(run_id)) is None
    # Nothing left driving it, and the engine says the run is over rather than
    # reporting one that is working with no row and nobody watching.
    assert driving == []
    assert snapshot is not None
    assert snapshot.status is RunStatus.FAILED
    assert snapshot.error == CANCELLED


def test_deleting_a_graph_work_order_the_engine_never_heard_of_still_works() -> None:
    """A row whose graph state is gone is still the reader's to throw away.

    The case `restore_graph_runs` fails a run for: the engine has no record of
    it, so there is nothing to cancel. Refusing the delete would leave a
    WorkOrder that cannot be removed and that nothing is working on.
    """
    store = InMemoryStateStore()
    app, runtime = _graph_app(store, _review_graph())
    stranded = RunState(
        run_id=RunId("run-stranded"),
        task_id=TaskId("task-stranded"),
        workflow_id=WorkflowId("implementation-review-codex"),
        phase=RunPhase.RUNNING_AGENT,
        prompt="Add cancellation handling.",
        repository="acme/api",
    )

    async def scenario():
        await store.save(stranded)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                deleted = await client.delete(f"/api/runs/{stranded.run_id}")
                return deleted, [str(one) for one in runtime.running()]

    deleted, driving = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert asyncio.run(store.load(stranded.run_id)) is None
    assert driving == []


def test_a_work_order_of_a_withdrawn_workflow_still_lists_and_still_reads() -> None:
    """A WorkOrder outlives the workflow it ran, and the pages have to cope.

    Renaming a graph -- what #367 did to this one -- or taking it out of the
    workflow directory leaves rows behind whose graph nothing can describe. The
    list is every WorkOrder there is, so a refusal let out of one row would take
    the whole page down, and the transcript is recorded against the run rather
    than against the graph, so it is still there to be read.

    The engine's own surface says the same thing with a 404: "there is no such
    graph" is an answer, and it used to be a `KeyError` on the way out.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                # Waited for rather than assumed: the node is scripted to say
                # something and then stop, and withdrawing the graph before it
                # had spoken would test a transcript that was never recorded.
                for _ in range(200):
                    feed = await client.get(f"/api/runs/{run_id}/graph-events")
                    if any(
                        one["type"] == "transcript" for one in feed.json()["events"]
                    ):
                        break
                    await asyncio.sleep(0.01)
                # The deployment stops defining the graph, with the run of it
                # left exactly where it was.
                runtime.withdraw(GraphId("implementation-review-codex"))
                return (
                    await client.get("/api/runs"),
                    await client.get(f"/api/runs/{run_id}"),
                    await client.get(f"/api/runs/{run_id}/graph-events"),
                    await client.get(f"/graph/api/runs/{run_id}"),
                    await client.get("/graph/api/graphs/implementation-review-codex"),
                )

    listed, detail, events, snapshot, described = asyncio.run(scenario())

    assert listed.status_code == 200
    assert [one["workflowId"] for one in listed.json()["runs"]] == [
        "implementation-review-codex"
    ]
    # Listed without a frontier rather than not listed: nothing can say where
    # the run got to, and that is not a reason to hide it.
    assert "graphProgress" not in listed.json()["runs"][0]
    assert detail.status_code == 200
    # What the run said is still readable, which is the difference between an
    # old WorkOrder being openable and being a dead link.
    assert events.status_code == 200
    assert [one["type"] for one in events.json()["events"]].count("transcript") == 1
    assert snapshot.status_code == 404
    assert described.status_code == 404


def test_a_restart_fails_a_work_order_whose_workflow_is_gone() -> None:
    """The row is told, rather than left claiming an agent is working on it.

    Nothing can pick this run back up -- there is no graph to run it -- so the
    honest ending is a failure naming the workflow that went missing. Left
    alone it would sit at "working" for as long as the deployment lives.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                while not runtime.running():
                    await asyncio.sleep(0)
        runtime.withdraw(GraphId("implementation-review-codex"))
        restarted = _graph_app_over(store, runtime)
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with restarted.router.lifespan_context(restarted):
                return await client.get(f"/api/runs/{run_id}")

    detail = asyncio.run(scenario()).json()

    assert detail["phase"] == "failed"
    assert "implementation-review-codex" in detail["failureReason"]
    assert "no longer available" in detail["failureReason"]


def test_the_graph_engine_answers_under_its_own_prefix() -> None:
    """Where a graph run is watched and approved today.

    This app's pages cannot do either yet, and the graph engine's own API can,
    so it is served from here rather than left unreachable. Behind `/graph`
    because both call their runs `/api/runs`.
    """
    app, _ = _graph_app(InMemoryStateStore(), _review_graph())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return await client.get("/graph/api/graphs")

    described = asyncio.run(scenario())

    assert described.status_code == 200
    assert [one["graphId"] for one in described.json()["graphs"]] == [
        "implementation-review-codex"
    ]


def test_a_failed_graph_run_says_why_on_its_row() -> None:
    """The other ending, and the reason that comes with it.

    The reason is read out of the event the engine publishes, so the row and
    the graph engine's own API give the same answer to "why did this stop?".
    A renamed key on that event would leave a failed WorkOrder with nothing to
    show, which is what this is here to catch.
    """
    store = InMemoryStateStore()
    broken = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Fail("codex is out of quota"),)),),
    )
    app, _ = _graph_app(store, broken)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                return (await _await_phase(client, run_id, "failed")).json()

    ended = asyncio.run(scenario())

    assert ended["phase"] == "failed"
    assert ended["failureReason"] == "codex is out of quota"


def test_messaging_a_failed_graph_implementer_resets_its_workorder() -> None:
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(
            NodeId("implementation"),
            (AwaitSteering(), Fail("codex is out of quota")),
            always_open=True,
        ),),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                async with asyncio.timeout(5):
                    while not (await runtime.snapshot(run_id)).active_executions:
                        await asyncio.sleep(0)
                response = await client.post(
                    f"/graph/api/runs/{run_id}/steering",
                    json={"node": "implementation", "message": "Start implementing."},
                )
                assert response.status_code == 200
                failed = (await _await_phase(client, run_id, "failed")).json()
                assert failed["phase"] == "failed"
                assert failed["failureReason"] == "codex is out of quota"

                restarted = await client.post(
                    f"/graph/api/runs/{run_id}/steering",
                    json={"node": "implementation", "message": "Try again."},
                )
                assert restarted.status_code == 200
                assert restarted.json()["status"] == "running"
                assert restarted.json()["error"] == ""
                return (await client.get(f"/api/runs/{run_id}")).json()

    restarted = asyncio.run(scenario())
    assert restarted["phase"] == "running_agent"
    assert restarted["failureReason"] == ""
    assert restarted["terminalOutcome"] is None


def test_a_graph_run_that_ends_before_its_row_exists_is_still_recorded() -> None:
    """The narrowest bit of ordering in the whole change.

    A graph short enough to be over before `start` answers announces its ending
    to nobody: there is no row yet for the announcement to land on. So the
    engine is asked once more after the row is saved, and this is the case that
    exists for -- a graph with no work in it at all.
    """
    store = InMemoryStateStore()
    instant = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation")),),
    )
    app, _ = _graph_app(store, instant)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )

    created = asyncio.run(scenario())

    # Whether the ending arrived before or after the row was saved, the answer
    # a person is handed is never "an agent is working" on a run that is over.
    assert created.json()["phase"] in {"succeeded", "running_agent"}
    assert created.status_code == 201


# --- a graph WorkOrder across a restart ----------------------------------------
#
# What a graph run keeps in the engine's files is where it got to. What it does
# not keep is the *driver* -- the task working through the graph -- because that
# lives in a process, and a process that stops takes its drivers with it.
#
# Driven against a double rather than the scripted engine, because the thing
# under test is a second process finding runs a first one left behind, and the
# scripted engine keeps everything in the process that started it: a run it
# knows about is, by construction, one it is still driving.


@dataclass
class _EngineAfterARestart:
    """A graph engine that remembers runs but is driving none of them.

    Everything the recovery pass calls, and nothing else. `resumed` is what a
    test asserts on: relaunching a stranded run is invisible in this app's own
    state, because the run carries on being a run that is working.
    """

    answers: dict[RunId, RunSnapshot | None]
    resumed: list[tuple[RunId, CheckpointId]] = field(default_factory=list)

    def observe(self, observer) -> None:
        self._observer = observer

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        return self.answers.get(run_id)

    async def resume_from(self, run_id: RunId, checkpoint_id: CheckpointId):
        self.resumed.append((run_id, checkpoint_id))
        return self.answers[run_id]

    def graphs(self) -> tuple:
        return ()


def _restarted(
    store: InMemoryStateStore, answers: dict[RunId, RunSnapshot | None]
) -> tuple[object, _EngineAfterARestart]:
    runtime = _EngineAfterARestart(answers)

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    app = _workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
        graph_runtime=running(),
    )
    return app, runtime


def _interrupted_run() -> RunState:
    return RunState(
        run_id=RunId("run-graph"),
        task_id=TaskId("task-graph"),
        workflow_id=WorkflowId("implementation-review-codex"),
        phase=RunPhase.RUNNING_AGENT,
        prompt="Add cancellation handling.",
        repository="acme/api",
    )


def _graph_snapshot(status: RunStatus, error: str = "") -> RunSnapshot:
    return RunSnapshot(
        run_id=RunId("run-graph"),
        graph_id=GraphId("implementation-review-codex"),
        status=status,
        checkpoint_id=CheckpointId("checkpoint-3"),
        error=error,
    )


def _after_a_restart(app, store: InMemoryStateStore, run: RunState) -> RunState:
    async def scenario():
        await store.save(run)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
        restored = await store.load(run.run_id)
        assert restored is not None
        return restored

    return asyncio.run(scenario())


def test_a_graph_run_interrupted_mid_execution_is_picked_back_up() -> None:
    """The reason this pass exists at all.

    A run that was working when the process died has no driver in the process
    that replaces it, and nothing else would build one: the step executor
    cannot -- a graph has no steps -- and the engine only builds one when a run
    is started or a decision arrives. So the run is sent back to the last
    position it saved and carried on from there.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(store, {RunId("run-graph"): _graph_snapshot(RunStatus.RUNNING)})

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == [(RunId("run-graph"), CheckpointId("checkpoint-3"))]
    # Still working, and still not the step executor's: a resumed graph run is
    # a graph run, and nothing here started a step for it.
    assert restored.phase is RunPhase.RUNNING_AGENT
    assert restored.failure_reason == ""


def test_a_graph_run_waiting_on_a_person_is_left_where_it_is() -> None:
    """The case that already worked, and must not be disturbed.

    A run parked on a question is picked back up by the answer, not by the
    restart. Resuming it here would throw the question away -- the execution
    that asked it is gone, so the person's answer would have nowhere to go.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(
        store, {RunId("run-graph"): _graph_snapshot(RunStatus.AWAITING_APPROVAL)}
    )

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == []
    assert restored.phase is RunPhase.RUNNING_AGENT


def test_a_graph_run_that_ended_while_the_server_was_down_catches_up() -> None:
    """An ending announced to a process that was not there to hear it.

    `graph_event` only moves a row while this process is running. A run that
    finished during a restart would otherwise be a row that says "working"
    about a run the engine considers over.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(
        store,
        {RunId("run-graph"): _graph_snapshot(RunStatus.FAILED, "the checkout vanished")},
    )

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == []
    assert restored.phase is RunPhase.FAILED
    assert restored.failure_reason == "the checkout vanished"


def test_a_graph_run_the_engine_has_forgotten_is_failed_rather_than_left_working() -> None:
    """State deleted from under a row -- `graph-state/` thrown away, say.

    Nothing can recover it and nobody will ever answer it, so it is failed with
    a reason. The alternative is a WorkOrder that claims to be working for as
    long as the database survives.
    """
    store = InMemoryStateStore()
    app, _ = _restarted(store, {})

    restored = _after_a_restart(app, store, _interrupted_run())

    assert restored.phase is RunPhase.FAILED
    assert "no record" in restored.failure_reason


def _app_over(store: InMemoryStateStore, engine):
    """The web app with a graph engine that behaves however a test needs."""
    return _workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
        graph_runtime=engine,
    )


def test_a_graph_that_does_not_compile_stops_the_server_and_names_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken definition is not something to carry on without.

    A graph that does not compile means a file in this deployment's workflow
    directory says something that is not a graph. Starting anyway would serve a
    deployment nobody configured, and the person who could fix it would find out
    the first time somebody picked the workflow. So startup fails.

    What is logged is the only way anybody learns which one: the graph's id and
    the reason it would not compile. "a graph failed to compile" is not
    actionable in a directory holding several.
    """
    store = InMemoryStateStore()

    @asynccontextmanager
    async def broken(_app=None):
        raise GraphCompilationError(
            GraphId("implementation-review-codex"),
            ValueError("node 'review' is not reachable from '__start__'"),
        )
        yield  # pragma: no cover -- unreachable, and required to make this a CM

    app = _app_over(store, broken())

    async def scenario():
        async with app.router.lifespan_context(app):  # pragma: no cover -- raises
            pass

    with caplog.at_level(logging.ERROR, logger="engine.apps.web.api"):
        with pytest.raises(GraphCompilationError):
            asyncio.run(scenario())

    assert "implementation-review-codex" in caplog.text
    assert "not reachable" in caplog.text


def test_a_graph_engine_that_will_not_open_does_not_take_the_app_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Everything else that can go wrong stays inside the graph feature.

    Opening the engine also creates a directory and opens two SQLite files, and
    those fail for reasons that are about this machine rather than about any
    graph: a state directory it cannot write, a checkpoint file another process
    is holding. None of them is a reason for chats and projects to go down, so
    the engine simply does not run here.

    The failure is logged, because it is the only place anybody could find out.
    """
    store = InMemoryStateStore()

    @asynccontextmanager
    async def refusing(_app=None):
        raise PermissionError("graph-state/: read-only file system")
        yield  # pragma: no cover -- unreachable, and required to make this a CM

    app = _app_over(store, refusing())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return (
                    await client.get("/api/config"),
                    await client.post(
                        "/api/runs",
                        json={
                            "workflowId": "implementation-review-codex",
                            "prompt": "Add cancellation handling.",
                            "repository": "acme/api",
                        },
                    ),
                    await client.get("/graph/api/graphs"),
                )

    with caplog.at_level(logging.ERROR, logger="engine.apps.web.api"):
        config, refused, graph = asyncio.run(scenario())

    # The application is up, and answering about everything it can still do.
    assert config.status_code == 200
    assert config.json()["workflows"] == []
    # Nothing offers the graph, so picking one is picking something that does
    # not exist rather than something that cannot be started.
    assert refused.status_code == 400
    assert graph.status_code == 503
    assert "read-only file system" in caplog.text


@pytest.mark.parametrize("values, status", [
    ({"implementation_runner": "claude", "review_runner": "codex"}, 201),
    ({}, 201),
    ({"review_runner": "unknown"}, 400),
    ({"review_runner": ""}, 400),
    ({"review_runner": 42}, 400),
    ({"undeclared": "value"}, 400),
    ([], 400),
])
def test_graph_workorder_inputs_are_validated_and_passed_to_execution(values, status):
    from dataclasses import dataclass
    from engine.graph_runtime.inputs import WorkflowInput

    @dataclass(frozen=True)
    class InputGraph(ScriptedGraph):
        inputs: tuple[WorkflowInput, ...] = (
            WorkflowInput("implementation_runner", "Implementation runner", "codex", True, ("codex", "claude")),
            WorkflowInput("review_runner", "Review runner", "claude", True, ("codex", "claude")),
        )

    graph = InputGraph(
        GraphId("inputs"), "Inputs",
        (ScriptedNode(NodeId("work"), (Say("Done"),)),),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                config = (await client.get("/api/config")).json()
                offered = next(item for item in config["workflows"] if item["id"] == "inputs")
                assert offered["inputs"][0]["choices"] == ["codex", "claude"]
                response = await client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Task",
                    "inputs": values,
                })
                assert response.status_code == status
                if status == 201:
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    assert snapshot.values["inputs"] == {
                        "implementation_runner": "codex", "review_runner": "claude", **values,
                    }
                else:
                    assert (await client.get("/api/runs")).json()["runs"] == []

    asyncio.run(scenario())


def test_scheduled_graph_workorder_survives_restart_and_starts_with_same_id() -> None:
    async def scenario():
        store = InMemoryStateStore()
        graph = ScriptedGraph(GraphId("scheduled-graph"), "Scheduled graph", (
            ScriptedNode(NodeId("work"), "Work", (AwaitSteering(),)),
        ))
        state = RunState(
            run_id=RunId("run-scheduled-graph"), task_id=TaskId("task-scheduled"),
            workflow_id=WorkflowId(str(graph.graph_id)), phase=RunPhase.SCHEDULED,
            name="Scheduled graph work", prompt="Do the work", repository=".",
        )
        await store.save(state)
        app, runtime = _graph_app(store, graph)
        async with app.router.lifespan_context(app):
            assert (await store.load(state.run_id)).phase is RunPhase.SCHEDULED
            assert await runtime.snapshot(state.run_id) is None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first, second = await asyncio.gather(
                    client.post("/api/runs/run-scheduled-graph/start"),
                    client.post("/api/runs/run-scheduled-graph/start"),
                )
                assert sorted([first.status_code, second.status_code]) == [200, 409]
                response = first if first.status_code == 200 else second
                assert (await client.post("/api/runs/missing/start")).status_code == 404
                assert len(await store.list_runs()) == 1
                assert response.json()["runId"] == state.run_id
                assert response.json()["phase"] != "scheduled"
                assert await runtime.snapshot(state.run_id) is not None
                assert (await client.post("/api/runs/run-scheduled-graph/start")).status_code == 409
    asyncio.run(scenario())
