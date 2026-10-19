"""Connected and disconnected runs: one vocabulary, read the same way everywhere."""

import pytest

from engine.domain import MODE_INPUT, ForgeMode, forge_mode
from engine.graph_runtime.inputs import mode_input, resolve_inputs
from engine.graph_runtime_langgraph.components.forge import (
    PUBLISH_CHANGE,
    PUBLISH_SUMMARY,
    ByMode,
)


@pytest.mark.parametrize(("inputs", "mode"), [
    (None, ForgeMode.CONNECTED),
    ({}, ForgeMode.CONNECTED),
    ({MODE_INPUT: "connected"}, ForgeMode.CONNECTED),
    ({MODE_INPUT: "disconnected"}, ForgeMode.DISCONNECTED),
    ({MODE_INPUT: "something else"}, ForgeMode.CONNECTED),
])
def test_a_run_is_connected_unless_its_inputs_say_otherwise(inputs, mode):
    assert forge_mode(inputs) is mode


def test_the_mode_input_offers_both_modes_and_rejects_others():
    declared = mode_input()
    assert declared.name == MODE_INPUT
    assert declared.choices == ("connected", "disconnected")
    assert resolve_inputs((declared,), {}) == {MODE_INPUT: "connected"}
    assert mode_input(ForgeMode.DISCONNECTED).default == "disconnected"
    with pytest.raises(ValueError):
        resolve_inputs((declared,), {MODE_INPUT: "offline"})


def test_snippets_are_worded_for_the_run_s_mode_and_formatted():
    snippet = ByMode(connected="post to {pr_url}", disconnected="keep it")
    assert snippet({}, pr_url="https://pr") == "post to https://pr"
    assert snippet({"inputs": {MODE_INPUT: "disconnected"}}, pr_url="x") == "keep it"
    offline = {"inputs": {MODE_INPUT: "disconnected"}}
    for text in (PUBLISH_CHANGE(offline), PUBLISH_SUMMARY(offline, pr_url="")):
        assert "open_pull_request" not in text and "add_comment" not in text
    assert "open_pull_request" in PUBLISH_CHANGE({})


@pytest.mark.parametrize(("login", "writable", "inputs", "mode", "reason"), [
    ("bot", True, {}, "connected", "write access"),
    ("", False, {}, "disconnected", "no forge connection"),
    ("bot", False, {}, "disconnected", "cannot write"),
    ("bot", True, {"mode": "disconnected"}, "disconnected", "explicitly requested"),
    ("bot", False, {"mode": "connected"}, "disconnected", "cannot write"),
])
def test_mode_is_inferred_from_the_backend_repository_access(login, writable, inputs, mode, reason):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.graph_runtime_langgraph.forge import resolve_forge_context

    source = SimpleNamespace(
        authenticated_login=AsyncMock(return_value=login),
        can_write_repository=AsyncMock(return_value=writable),
    )
    context = asyncio.run(resolve_forge_context(source, {"repository": "acme/api", "inputs": inputs}))
    assert context["mode"] == mode
    assert reason in context["reason"]
    assert context["summary"].startswith(mode + " · ")
    if inputs.get("mode") == "disconnected":
        source.authenticated_login.assert_not_called()
    elif login:
        source.can_write_repository.assert_awaited_once_with("https://github.com/acme/api", "bot")


def test_every_graph_accepts_the_local_override_without_declaring_it():
    assert resolve_inputs((), {"mode": "disconnected"}) == {"mode": "disconnected"}
    with pytest.raises(ValueError):
        resolve_inputs((), {"mode": "offline"})


def test_context_is_attached_before_nodes_and_survives_a_store_restart(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.domain import RunId
    from engine.graph_runtime import GraphId
    from engine.graph_runtime_langgraph import LangGraphDefinition, LangGraphRuntime, SqliteGraphRuntimeStore
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END

    async def scenario():
        observed = []
        async def node(state):
            observed.append(state["inputs"]["mode"])
            return state
        graph = StateGraph(dict)
        graph.add_node("work", node)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        source = SimpleNamespace(authenticated_login=AsyncMock(return_value="bot"),
                                 can_write_repository=AsyncMock(return_value=True))
        store = SqliteGraphRuntimeStore(tmp_path / "runs.db")
        runtime = LangGraphRuntime(LangGraphDefinition(GraphId("test"), "Test", graph.compile(checkpointer=InMemorySaver())),
                                   store=store, source_control=source)
        try:
            started = await runtime.start(GraphId("test"), {"repository": "acme/api"})
            for _ in range(100):
                snapshot = await runtime.snapshot(started.run_id)
                if snapshot.status.value == "completed":
                    break
                await asyncio.sleep(0.01)
            assert observed == ["connected"]
            assert snapshot.forge_context["mode"] == "connected"
        finally:
            await runtime.aclose()
            store.close()
        reopened = SqliteGraphRuntimeStore(tmp_path / "runs.db")
        try:
            assert (await reopened.run(RunId(started.run_id))).forge_context == started.forge_context
        finally:
            reopened.close()
    asyncio.run(scenario())


def test_graph_api_connected_run_opens_pr_and_records_review_comments(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from engine.domain import ApprovalDecision, RunId
    from engine.graph_runtime import GraphId
    from engine.graph_runtime.api import create_app
    from engine.graph_runtime_langgraph import LangGraphDefinition, LangGraphRuntime, TerminalMcpServer, current_execution
    from engine.ports import CommentResult
    from engine.runtime.terminal_mcp import _mcp_response
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END

    url = "https://github.com/acme/api/pull/7"
    source = SimpleNamespace(
        authenticated_login=AsyncMock(return_value="bot"),
        can_write_repository=AsyncMock(return_value=True),
        request_review=AsyncMock(return_value=url),
        add_comment=AsyncMock(return_value=CommentResult(123, url + "#c123")),
    )

    async def approve(_request):
        return ApprovalDecision.ACCEPT

    async def publish(state):
        server = TerminalMcpServer(step_id="publish", agent_id="test",
                                   repository_tools=("open_pull_request", "add_comment"))
        async with server(state, current_execution(), approve) as bound:
            args = list(bound.config["args"])
            async def call(name, arguments):
                reply = await _mcp_response(
                    args[args.index("--host") + 1], int(args[args.index("--port") + 1]),
                    args[args.index("--token") + 1],
                    {"jsonrpc": "2.0", "id": name, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}},
                    repository_tools=server.repository_tools,
                )
                assert not reply["result"].get("isError"), reply
                return reply["result"]["content"][0]["text"]
            result = await call("open_pull_request", {"branch": "agent/change", "title": "feat: change", "body": "Change"})
            assert url in result
            await call("add_comment", {"pr_url": url, "comment": "**Finding**\n\nDetails\n\n_Produced by test reviewing bugs_"})
        return {**state, "pr_url": url}

    async def scenario():
        graph = StateGraph(dict)
        graph.add_node("publish", publish)
        graph.add_edge(START, "publish")
        graph.add_edge("publish", END)
        runtime = LangGraphRuntime(LangGraphDefinition(GraphId("test"), "Test", graph.compile(checkpointer=InMemorySaver())),
                                   source_control=source)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://test") as client:
                response = await client.post("/api/runs", json={"graphId": "test", "values": {"repository": "acme/api", "workspaceId": "ws-test"}})
                assert response.status_code == 201
                run_id = RunId(response.json()["runId"])
                for _ in range(100):
                    snapshot = await runtime.snapshot(run_id)
                    if snapshot.status.value in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                assert snapshot.status.value == "completed", snapshot.error
                assert snapshot.forge_context["mode"] == "connected"
                assert await runtime.store.pull_requests(run_id) == (("acme/api", 7),)
                comments = await runtime.store.comments(run_id)
                assert len(comments) == 1 and comments[0].node_id == "publish"
                source.request_review.assert_awaited_once()
                source.add_comment.assert_awaited_once()
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_disconnected_run_keeps_commits_on_a_descriptive_local_branch(tmp_path, git_repo):
    import asyncio
    from pathlib import Path
    from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
    from engine.domain import ApprovalDecision
    from engine.graph_runtime import GraphId
    from engine.graph_runtime_langgraph import LangGraphDefinition, LangGraphRuntime, TerminalMcpServer, WorkspaceNode, current_execution
    from engine.ports import GitResult
    from engine.runtime.terminal_mcp import _mcp_response
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END

    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    (repository / "new-local.txt").write_text("uncommitted local input\n")
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "workspaces"))

    class LocalGit:
        async def run_git(self, workspace_id, arguments):
            root = await provider.root_path(workspace_id)
            process = await asyncio.create_subprocess_exec(
                "git", "-C", root, "-c", "user.name=Engine Tests",
                "-c", "user.email=engine@example.test", *arguments,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await process.communicate()
            return GitResult(process.returncode, out.decode(), err.decode())

    async def approve(_request):
        return ApprovalDecision.ACCEPT

    async def implement(state):
        assert state["inputs"]["mode"] == "disconnected"
        assert Path(state["workspace"], "new-local.txt").read_text() == "uncommitted local input\n"
        Path(state["workspace"], "change.txt").write_text("local change\n")
        server = TerminalMcpServer(step_id="implement", agent_id="test", required_outputs=("pr_url",))
        async with server(state, current_execution(), approve) as bound:
            args = list(bound.config["args"])
            for arguments in (["switch", "-c", "agent/local-change"], ["add", "change.txt", "new-local.txt"],
                              ["commit", "-m", "feat: local change"]):
                reply = await _mcp_response(
                    args[args.index("--host") + 1], int(args[args.index("--port") + 1]),
                    args[args.index("--token") + 1],
                    {"jsonrpc": "2.0", "id": arguments[0], "method": "tools/call",
                     "params": {"name": "git_subcommand", "arguments": {"arguments": arguments}}},
                    repository_tools=("git_subcommand",),
                )
                assert not reply["result"].get("isError"), reply
        return {**state, "branch": "agent/local-change"}

    async def scenario():
        graph = StateGraph(dict)
        # A dict state replaces on update: preserve the run inputs between nodes.
        async def workspace(state):
            return {**state, **await WorkspaceNode(provider)(state)}
        graph.add_node("workspace", workspace)
        graph.add_node("implement", implement)
        graph.add_edge(START, "workspace")
        graph.add_edge("workspace", "implement")
        graph.add_edge("implement", END)
        source = LocalGit()
        runtime = LangGraphRuntime(LangGraphDefinition(GraphId("local"), "Local", graph.compile(checkpointer=InMemorySaver())), source_control=source)
        try:
            run = await runtime.start(GraphId("local"), {"repository": str(repository), "inputs": {"mode": "disconnected"}})
            for _ in range(500):
                snapshot = await runtime.snapshot(run.run_id)
                if snapshot.status.value in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert snapshot.status.value == "completed", snapshot.error
            workspace_id = snapshot.values["workspaceId"]
            assert (await source.run_git(workspace_id, ["status", "--porcelain"])).stdout == ""
            assert (await source.run_git(workspace_id, ["branch", "--show-current"])).stdout.strip() == "agent/local-change"
            assert (await source.run_git(workspace_id, ["show", "HEAD:change.txt"])).stdout == "local change\n"
            assert await runtime.store.pull_requests(run.run_id) == ()
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


def test_yaml_completion_requires_a_pr_only_when_connected():
    from engine.domain import AgentRunId, RunId, StepCompleted, StepId
    from engine.graph_service.compile import GraphTerminalMcpServer
    from engine.graph_service.language import OutputSpec

    server = GraphTerminalMcpServer(step_id="publish", agent_id="test",
        required_outputs=("pr_url",), outputs=(OutputSpec("pr_url", required=True),))
    completed = StepCompleted(RunId("run"), StepId("publish"), AgentRunId("agent"), "success", "Committed locally")
    local = server.for_mode(ForgeMode.DISCONNECTED)
    assert local.required_outputs == ()
    local.validate_completion(completed)
    with pytest.raises(ValueError, match="pr_url is required"):
        server.for_mode(ForgeMode.CONNECTED).validate_completion(completed)


@pytest.mark.parametrize(("level", "mode"), [(30, "connected"), (20, "disconnected")])
@pytest.mark.parametrize("host", ["gitlab.com", "gitlab.example.com"])
def test_gitlab_mode_inference_uses_real_adapter(level, mode, host):
    import asyncio
    from unittest.mock import AsyncMock
    from engine.adapters.source_control.gitlab import GitLabSourceControl
    from engine.graph_runtime_langgraph.forge import resolve_forge_context

    transport = AsyncMock()
    transport.request.side_effect = [
        {"username": "bot"},
        [{"username": "bot", "id": 4, "access_level": level}],
    ]
    source = GitLabSourceControl("token", origin=f"https://{host}", transport=transport)
    context = asyncio.run(resolve_forge_context(
        source, {"repository": f"https://{host}/group/subgroup/project.git"},
    ))
    assert context["mode"] == mode
    assert transport.request.await_args_list[1].args == (
        "GET", "/projects/group%2Fsubgroup%2Fproject/members/all",
    )


def test_gitlab_mode_inference_fails_closed_when_access_probe_fails():
    import asyncio
    from unittest.mock import AsyncMock
    from engine.adapters.source_control.gitlab import GitLabSourceControl
    from engine.adapters.source_control.gitlab.transports import GitLabTransportError
    from engine.graph_runtime_langgraph.forge import resolve_forge_context

    transport = AsyncMock()
    transport.request.side_effect = [
        {"username": "bot"}, GitLabTransportError("permission denied"),
    ]
    source = GitLabSourceControl("token", transport=transport)
    context = asyncio.run(resolve_forge_context(
        source, {"repository": "https://gitlab.com/group/project"},
    ))
    assert context["mode"] == "disconnected"
    assert context["reason"] == "forge access could not be verified"


def test_local_checkout_without_forge_runs_disconnected(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.graph_runtime_langgraph.forge import resolve_forge_context

    process = SimpleNamespace(communicate=AsyncMock(return_value=(b'', b'')))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    source = SimpleNamespace(authenticated_login=AsyncMock(), can_write_repository=AsyncMock())
    context = asyncio.run(resolve_forge_context(source, {'repository': str(tmp_path)}))
    assert context['mode'] == 'disconnected'
    assert context['reason'] == 'repository has no forge remote'
    source.authenticated_login.assert_not_called()
