"""Planner milestone tools, their data objects, and MCP presentation."""

import asyncio
import json

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentInstance,
    AgentInstanceId,
    AgentProfile,
    ConversationId,
    Message,
    MilestoneId,
    Project,
    ProjectId,
    project_id_for_instance,
)
from engine.ports import AgentTurn
from engine.runtime import AgentSession, Capabilities
from engine.runtime.planning_tools import (
    PLANNING_TOOL_NAMES,
    PlanningMcpBroker,
    PlanningTools,
    ProjectPlan,
    _mcp_response,
    project_chat_capabilities,
)
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


def test_only_project_backed_chats_receive_milestone_capabilities() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        project_chat = AgentInstance(
            AgentInstanceId("agi-project"),
            AgentId("coder"),
            ConversationId("conversation-project"),
        )
        ordinary_chat = AgentInstance(
            AgentInstanceId("agi-ordinary"),
            AgentId("planner"),
            ConversationId("conversation-ordinary"),
        )
        await store.save_project(
            Project(project_id_for_instance(project_chat.instance_id), "OpenEngine")
        )

        assert await project_chat_capabilities(store, project_chat) == (
            "add_milestone",
            "list_milestones",
            "update_milestone",
            "delete_milestone",
        )
        assert await project_chat_capabilities(store, ordinary_chat) == ()

    asyncio.run(scenario())


def test_planning_tools_create_present_update_and_delete_milestone_objects() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        project = Project(ProjectId("project-engine"), "OpenEngine")
        await store.save_project(project)
        tools = PlanningTools(store)

        foundation = await tools.add_milestone(
            project.project_id,
            "Foundation",
            "Persist the planning hierarchy.",
        )
        launch = await tools.add_milestone(
            project.project_id,
            "Launch",
            "Put the first release in users' hands.",
            (foundation.milestone_id,),
        )

        plan = await tools.list_milestones(project.project_id)
        assert plan == ProjectPlan(project, (foundation, launch))
        assert f"**Launch** (`{launch.milestone_id}`)" in plan.render()
        assert f"Depends on: `{foundation.milestone_id}`" in plan.render()
        assert plan.to_dict()["milestones"] == [
            {
                "milestone_id": foundation.milestone_id,
                "project_id": project.project_id,
                "name": "Foundation",
                "description": "Persist the planning hierarchy.",
                "dependencies": [],
            },
            {
                "milestone_id": launch.milestone_id,
                "project_id": project.project_id,
                "name": "Launch",
                "description": "Put the first release in users' hands.",
                "dependencies": [foundation.milestone_id],
            },
        ]

        updated = await tools.update_milestone(
            launch.milestone_id,
            name="Public launch",
            description="Release the stable product.",
            dependencies=(),
        )
        assert updated.name == "Public launch"
        assert updated.description == "Release the stable product."
        assert updated.dependencies == ()
        assert await tools.delete_milestone(foundation.milestone_id) == foundation
        assert await store.load_milestone(foundation.milestone_id) is None

    asyncio.run(scenario())


def test_planning_tools_reject_dangling_cross_project_and_cyclic_dependencies() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        first = Project(ProjectId("project-first"), "First")
        second = Project(ProjectId("project-second"), "Second")
        await store.save_project(first)
        await store.save_project(second)
        tools = PlanningTools(store)
        one = await tools.add_milestone(first.project_id, "One", "First milestone.")
        two = await tools.add_milestone(
            first.project_id, "Two", "Second milestone.", (one.milestone_id,)
        )
        foreign = await tools.add_milestone(
            second.project_id, "Foreign", "Another project."
        )

        with pytest.raises(ValueError, match="no dependency milestone"):
            await tools.add_milestone(
                first.project_id,
                "Broken",
                "References nothing.",
                (MilestoneId("milestone-missing"),),
            )
        with pytest.raises(ValueError, match="another project"):
            await tools.update_milestone(
                two.milestone_id, dependencies=(foreign.milestone_id,)
            )
        with pytest.raises(ValueError, match="cycle"):
            await tools.update_milestone(
                one.milestone_id, dependencies=(two.milestone_id,)
            )
        with pytest.raises(ValueError, match="required by"):
            await tools.delete_milestone(one.milestone_id)

    asyncio.run(scenario())


def test_planning_mcp_lists_the_four_tools_and_returns_structured_objects() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        instance = AgentInstance(
            AgentInstanceId("planner-1"),
            AgentId("planner"),
            ConversationId("conversation-1"),
        )
        project = Project(
            project_id_for_instance(instance.instance_id), "OpenEngine"
        )
        await store.save_project(project)
        broker = PlanningMcpBroker(store, PLANNING_TOOL_NAMES, instance)
        async with broker:
            listed = await _mcp_response(
                "127.0.0.1",
                1,
                "unused",
                PLANNING_TOOL_NAMES,
                {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            )
            config = broker.config
            token = config.args[config.args.index("--token") + 1]
            port = int(config.args[config.args.index("--port") + 1])
            added = await _mcp_response(
                "127.0.0.1",
                port,
                token,
                PLANNING_TOOL_NAMES,
                {
                    "jsonrpc": "2.0",
                    "id": "add-1",
                    "method": "tools/call",
                    "params": {
                        "name": "add_milestone",
                        "arguments": {
                            "name": "Launch",
                            "description": "Ship it.",
                            "dependencies": [],
                        },
                    },
                },
            )

        assert listed is not None
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "add_milestone",
            "list_milestones",
            "update_milestone",
            "delete_milestone",
        ]
        assert added is not None
        result = added["result"]
        assert result["content"][0]["text"].startswith("Added milestone")
        assert result["structuredContent"]["name"] == "Launch"
        assert result["structuredContent"]["dependencies"] == []

    asyncio.run(scenario())


def test_planning_mcp_rejects_cross_project_milestone_access() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        first_instance = AgentInstance(
            AgentInstanceId("planner-first"),
            AgentId("planner"),
            ConversationId("conversation-first"),
        )
        second_instance = AgentInstance(
            AgentInstanceId("planner-second"),
            AgentId("planner"),
            ConversationId("conversation-second"),
        )
        first_project = Project(
            project_id_for_instance(first_instance.instance_id), "First"
        )
        second_project = Project(
            project_id_for_instance(second_instance.instance_id), "Second"
        )
        await store.save_project(first_project)
        await store.save_project(second_project)
        foreign = await PlanningTools(store).add_milestone(
            second_project.project_id, "Foreign", "Belongs to the second project."
        )

        broker = PlanningMcpBroker(store, PLANNING_TOOL_NAMES, first_instance)
        listed = await _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            PLANNING_TOOL_NAMES,
            {"jsonrpc": "2.0", "id": "list-tools", "method": "tools/list"},
        )
        assert listed is not None
        list_tool = next(
            tool
            for tool in listed["result"]["tools"]
            if tool["name"] == "list_milestones"
        )
        assert list_tool["inputSchema"] == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        _, own_plan = await broker._call("list_milestones", {})
        assert own_plan == {
            "project": {
                "project_id": first_project.project_id,
                "name": "First",
            },
            "milestones": [],
        }
        with pytest.raises(ValueError, match="unexpected arguments: project_id"):
            await broker._call(
                "list_milestones", {"project_id": second_project.project_id}
            )
        with pytest.raises(ValueError, match="another project"):
            await broker._call(
                "update_milestone",
                {"milestone_id": foreign.milestone_id, "name": "Compromised"},
            )
        with pytest.raises(ValueError, match="another project"):
            await broker._call(
                "delete_milestone", {"milestone_id": foreign.milestone_id}
            )
        assert await store.load_milestone(foreign.milestone_id) == foreign

    asyncio.run(scenario())


def test_planner_turn_launches_scoped_stdio_mcp_and_forwards_a_call() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        advertised: list[str] = []

        class StdioMcpRunner:
            permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

            async def run_turn(
                self, agent_run_id, profile, messages, tools=(), workspace_id=None
            ):
                raise AssertionError("the MCP runner method should be used")

            async def cancel(self, agent_run_id) -> None:
                pass

            async def run_turn_with_mcp(
                self,
                agent_run_id,
                profile,
                messages,
                mcp_server,
                workspace_id=None,
            ):
                process = await asyncio.create_subprocess_exec(
                    mcp_server.command,
                    *mcp_server.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdin is not None
                assert process.stdout is not None
                assert process.stderr is not None

                async def exchange(request: dict[str, object]) -> dict[str, object]:
                    process.stdin.write(
                        json.dumps(request, separators=(",", ":")).encode() + b"\n"
                    )
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
                    assert line, "the stdio MCP server exited without a response"
                    return json.loads(line)

                try:
                    initialized = await exchange(
                        {
                            "jsonrpc": "2.0",
                            "id": "initialize-1",
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "test-runner", "version": "1"},
                            },
                        }
                    )
                    assert initialized["result"]["serverInfo"]["name"] == (
                        "engine-planning"
                    )
                    listed = await exchange(
                        {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"}
                    )
                    advertised.extend(
                        tool["name"] for tool in listed["result"]["tools"]
                    )
                    denied = await exchange(
                        {
                            "jsonrpc": "2.0",
                            "id": "delete-1",
                            "method": "tools/call",
                            "params": {
                                "name": "delete_milestone",
                                "arguments": {"milestone_id": "milestone-anything"},
                            },
                        }
                    )
                    assert denied["result"]["isError"] is True
                    added = await exchange(
                        {
                            "jsonrpc": "2.0",
                            "id": "add-1",
                            "method": "tools/call",
                            "params": {
                                "name": "add_milestone",
                                "arguments": {
                                    "name": "Foundation",
                                    "description": "Build the planning model.",
                                },
                            },
                        }
                    )
                    assert added["result"]["structuredContent"]["name"] == (
                        "Foundation"
                    )
                finally:
                    process.stdin.close()
                    await process.stdin.wait_closed()
                    return_code = await asyncio.wait_for(process.wait(), timeout=5)
                    stderr = (await process.stderr.read()).decode()
                    assert return_code == 0, stderr
                return AgentTurn(Message.assistant("Foundation milestone added."))

        planner = AgentId("planner")
        session = AgentSession(
            Capabilities(
                workflow_runtime=None,
                source_control=None,
                agent_runner=StdioMcpRunner(),
                communications=None,
                workspace_provider=None,
                state_store=store,
            ),
            profiles={
                planner: AgentProfile(
                    planner,
                    "Plan it.",
                    capabilities=("add_milestone",),
                )
            },
            mcp_brokers={name: PlanningMcpBroker for name in PLANNING_TOOL_NAMES},
        )
        instance = await session.start(planner)
        project = Project(
            project_id_for_instance(instance.instance_id), "OpenEngine"
        )
        await store.save_project(project)

        turn = await session.say(instance.instance_id, "Add the foundation.")

        assert turn.message.content == "Foundation milestone added."
        assert advertised == ["add_milestone"]
        milestones = await store.list_milestones(project.project_id)
        assert [milestone.name for milestone in milestones] == ["Foundation"]

    asyncio.run(scenario())
