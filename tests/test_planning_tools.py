"""Planner milestone tools, their data objects, and MCP presentation."""

import asyncio

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import MilestoneId, Project, ProjectId
from engine.runtime.planning_tools import (
    PlanningMcpBroker,
    PlanningTools,
    ProjectPlan,
    _mcp_response,
)


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
        project = Project(ProjectId("project-engine"), "OpenEngine")
        await store.save_project(project)
        broker = PlanningMcpBroker(store)
        async with broker:
            listed = await _mcp_response(
                "127.0.0.1",
                1,
                "unused",
                {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            )
            config = broker.config
            token = config.args[config.args.index("--token") + 1]
            port = int(config.args[config.args.index("--port") + 1])
            added = await _mcp_response(
                "127.0.0.1",
                port,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": "add-1",
                    "method": "tools/call",
                    "params": {
                        "name": "add_milestone",
                        "arguments": {
                            "project_id": project.project_id,
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
