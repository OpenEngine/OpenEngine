"""Registered child calls use real run records and checkpoints."""

import asyncio

import pytest
import yaml

from engine.domain import RunId
from engine.graph_service import GraphError, parse_graph
from engine.graph_service.compile import GraphAgentNode, render_inputs
from test_graph_service import graph_service, paused, settled


CHILD = """apiVersion: openengine.cc/v1
name: child
inputs:
  context: {required: true}
review:
  report:
    agent: stub
    prompt: ${inputs.context}
    outputs:
      findings: {type: findings, required: true}
"""


def parent(reference="child", inputs=None):
    return yaml.safe_dump({
        "apiVersion": "openengine.cc/v1", "name": "parent",
        "inputs": {"context": {"default": "inspect"}},
        "review": {
            "call": {"graph": reference, "inputs": inputs if inputs is not None else {"context": "${inputs.context}"}},
            "consume": {"agent": "stub", "prompt": "${json(outputs.call.findings)}"},
        },
    }, sort_keys=False)


def test_graph_call_parser_and_input_expressions():
    spec = parse_graph(yaml.safe_load(parent()))
    assert spec.node("call").kind == "graph"
    assert render_inputs({"value": "${outputs.call.findings}"}, {"outputs": {"call": {"findings": []}}}) == {"value": []}
    with pytest.raises(GraphError, match="not a declared input"):
        parse_graph(yaml.safe_load(parent(inputs={"context": "${inputs.missing}"})))
    with pytest.raises(GraphError, match="only one"):
        raw = yaml.safe_load(parent())
        raw["review"]["call"]["agent"] = "stub"
        parse_graph(raw)


@pytest.mark.parametrize("reference,inputs,message", [
    ("missing", {}, "no graph named"),
    ("child", {"typo": "x"}, "not a declared child input"),
    ("child", {}, "required child input"),
    ("parent", {}, "parent → parent"),
])
def test_registration_validates_child_contract(tmp_path, reference, inputs, message):
    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph("project", source=CHILD)
            with pytest.raises(GraphError, match=message):
                await service.add_graph("project", source=parent(reference, inputs))
    asyncio.run(scenario())


def test_registration_rejects_indirect_recursion_and_cross_project(tmp_path):
    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph("project", source=CHILD)
            await service.add_graph("project", source=parent())
            recursive = yaml.safe_load(CHILD)
            recursive["review"] = {"again": {"graph": "parent"}}
            with pytest.raises(GraphError, match="child → parent → child"):
                await service.add_graph("project", source=yaml.safe_dump(recursive))
            other, _ = await service.add_graph("other", source=CHILD)
            with pytest.raises(GraphError, match="same project"):
                await service.add_graph("project", source=parent(other["versionId"]))
    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_child_outputs_failure_linkage_and_workspace(tmp_path, monkeypatch, fail):
    seen = []

    async def agent(self, state):
        seen.append(dict(state))
        if self.state_key == "report":
            if fail:
                message = "child review failed"
                raise ValueError(message)
            return {"report": {"findings": []}}
        assert self.prompt(state) == "[]"
        return {"consume": "consumed findings"}

    monkeypatch.setattr(GraphAgentNode, "__call__", agent)

    async def scenario():
        async with graph_service(tmp_path) as service:
            child_graph, _ = await service.add_graph("project", source=CHILD)
            await service.add_graph("project", source=parent(inputs={
                "context": "${inputs.context}", "instruction": "child instruction",
            }))
            run, _ = await service.submit_run(project="project", graph="parent", instruction="review",
                                              inputs={"mode": "disconnected"})
            run = await settled(service, run["runId"])
            child_id = run["childRuns"][0]["runId"]
            child = await service.run_json(child_id)
            assert child["parentRunId"] == run["runId"]
            assert child["versionId"] == child_graph["versionId"]
            assert run["childRuns"][0]["step"] == "call"
            record = await service.runtime.store.run(RunId(child_id))
            assert record.metadata.parent_run_id == RunId(run["runId"])
            assert child["workspace"] == run["workspace"]
            if fail:
                assert run["status"] == "failed"
                assert "child review failed" in run["failure"]["error"]
                assert child_id in run["failure"]["error"]
            else:
                assert run["status"] == "completed"
                assert run["results"]["call"]["findings"] == []
                assert run["results"]["consume"] == "consumed findings"
                assert seen[0]["inputs"]["context"] == "inspect"
                assert seen[0]["inputs"]["mode"] == "disconnected"
                assert seen[0]["task"] == "child instruction"
    asyncio.run(scenario())


@pytest.mark.parametrize("explicit", [False, True])
def test_versions_are_pinned_before_parent_executes(tmp_path, monkeypatch, explicit):
    entered, release = None, None

    async def agent(self, state):
        if self.state_key == "gate":
            entered.set()
            await release.wait()
        return {self.state_key: {"findings": []}}

    monkeypatch.setattr(GraphAgentNode, "__call__", agent)

    async def scenario():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        async with graph_service(tmp_path) as service:
            first, _ = await service.add_graph("project", source=CHILD)
            raw = yaml.safe_load(parent("child@1" if explicit else "child"))
            if explicit:
                await service.add_graph("project", source=CHILD.replace("${inputs.context}", "second"))
            raw["plan"] = {"gate": {"agent": "stub", "prompt": "wait"}}
            await service.add_graph("project", source=yaml.safe_dump(raw, sort_keys=False))
            run, _ = await service.submit_run(project="project", graph="parent", instruction="review")
            await asyncio.wait_for(entered.wait(), 5)
            await service.add_graph("project", source=CHILD.replace("${inputs.context}", "new version"))
            release.set()
            run = await settled(service, run["runId"])
            assert (await service.run_json(run["childRuns"][0]["runId"]))["versionId"] == first["versionId"]
    asyncio.run(scenario())


def test_parent_cancellation_cancels_child(tmp_path, monkeypatch):
    started = None

    async def agent(self, state):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(GraphAgentNode, "__call__", agent)

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        async with graph_service(tmp_path) as service:
            await service.add_graph("project", source=CHILD)
            await service.add_graph("project", source=parent())
            run, _ = await service.submit_run(project="project", graph="parent", instruction="review")
            await asyncio.wait_for(started.wait(), 5)
            cancelled = await service.cancel(run["runId"])
            child = await settled(service, cancelled["childRuns"][0]["runId"])
            assert child["status"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["spend", "prs"])
def test_loop_limits_count_child_usage(tmp_path, monkeypatch, limit):
    from engine.graph_runtime.events import EventKind
    from engine.graph_runtime_langgraph import PullRequestRecord, current_execution

    async def agent(self, state):
        execution = current_execution()
        if self.state_key == "report":
            if limit == "spend":
                await execution.runtime.publish(execution.run_id, EventKind.USAGE_UPDATED, {
                    "sessionId": "child-session", "agent": "stub", "sessionCostUsd": 0.5,
                }, node_id=execution.node_id, execution_id=execution.execution_id)
                await asyncio.Event().wait()
            await execution.runtime.store.remember_pull_request(PullRequestRecord(
                "example/repo", 7, execution.run_id, "2026-10-05T12:00:00+00:00",
            ))
        return {self.state_key: {"findings": []}}

    monkeypatch.setattr(GraphAgentNode, "__call__", agent)

    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph("project", source=CHILD)
            await service.add_graph("project", source=parent())
            loop = await service.add_loop(
                project="project", graph="parent", instruction="review", every="1h",
                **({"max_spend_usd": 0.25} if limit == "spend" else {"max_prs": 1}),
            )
            await service.tick()
            loop = await paused(service, loop["loopId"])
            assert loop["pauseReason"].startswith("max-spend" if limit == "spend" else "max-prs")
            if limit == "spend":
                assert loop["spend"]["usd"] == 0.5
                run = await settled(service, loop["activeRunId"])
                assert run["status"] == "cancelled"
                assert (await settled(service, run["childRuns"][0]["runId"]))["status"] == "cancelled"
            else:
                assert loop["prCount"] == 1
    asyncio.run(scenario())


def test_child_human_step_uses_normal_approval_feed(tmp_path):
    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph("project", source="""apiVersion: openengine.cc/v1
name: child
review:
  approve:
    human: Accept these findings?
""")
            raw = yaml.safe_load(parent(inputs={}))
            del raw["review"]["consume"]
            await service.add_graph("project", source=yaml.safe_dump(raw))
            run, _ = await service.submit_run(project="project", graph="parent", instruction="review")
            for _ in range(100):
                parent_run = await service.run_json(run["runId"])
                children = parent_run["childRuns"]
                if children and children[0]["status"] == "awaiting_approval":
                    break
                await asyncio.sleep(0.05)
            child = await service.run_json(children[0]["runId"])
            assert child["pendingApprovals"]
            await service.decide(child["runId"], child["pendingApprovals"][0]["approvalId"], "accept")
            assert (await settled(service, run["runId"]))["status"] == "completed"
    asyncio.run(scenario())
