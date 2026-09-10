"""Structured review outputs survive parallel graph execution with lineage."""

import asyncio
import json

import pytest
from langgraph.graph import END, START, StateGraph

from engine.domain import AgentRunId, RunId, StepCompleted, StepId, StepOutput
from engine.graph_runtime_langgraph.components import Finding, ReviewNode, RerankerNode
from engine.graph_runtime_langgraph.workflows import State
from test_graph_workflow_definitions import definition_module, nodes_of


def completed(findings):
    return StepCompleted(
        run_id=RunId("run"), step_id=StepId("review"),
        agent_run_id=AgentRunId("agent"), outcome="success",
        summary="Summary is deliberately not the findings.",
        outputs=(StepOutput("findings", json.dumps(findings)),),
    )


def test_finding_round_trip_and_comment():
    finding = Finding("Users lose their work", "Saving overwrites an existing file.",
                      "bugs", "claude", "save.py", 7)
    assert Finding.from_dict(finding.to_dict()) == finding
    assert finding.as_comment() == (
        "**Users lose their work**\n\nSaving overwrites an existing file.\n\n"
        "_Produced by agent: claude, facet: bugs_"
    )
    minimal = Finding("Work is lost", "Saving deletes the previous version.")
    assert minimal.to_dict() == {
        "tagline": minimal.tagline, "description": minimal.description,
    }
    assert "Produced by" not in minimal.as_comment()


@pytest.mark.parametrize("data", [
    {}, {"tagline": None, "description": "Bad"},
    {"tagline": " ", "description": "Bad"},
    {"tagline": "a\nb\nc", "description": "Bad"},
    {"tagline": "Bad", "description": "a\nb\nc\nd"},
    {"tagline": "Bad", "description": "Bad", "line": 0},
    {"tagline": "Bad", "description": "Bad", "line": True},
])
def test_invalid_finding_is_rejected(data):
    with pytest.raises(ValueError):
        Finding.from_dict(data)


@pytest.mark.parametrize("value", ["not json", "{}", "[1]"])
def test_review_rejects_invalid_output(value):
    node = ReviewNode(agent="claude", facet="bugs", cwd="/tmp", output_key="review-bugs")
    event = completed([])
    from dataclasses import replace
    with pytest.raises(ValueError):
        node._terminal_update(replace(event, outputs=(StepOutput("findings", value),)))


@pytest.mark.parametrize("runner,reviewer,default,security", [
    ("codex", "claude", "claude-sonnet-5", "claude-opus-5"),
    ("claude", "codex", "gpt-5.6-terra", "gpt-5.6-sol"),
])
def test_cross_provider_and_model_tiers(runner, reviewer, default, security):
    nodes = nodes_of(definition_module().pipeline(runner))
    assert nodes["implementation"].agent == runner
    for facet in ("security", "bugs", "performance", "conciseness"):
        node = nodes[f"review-{facet}"]
        assert node.agent == reviewer
        assert node.session_config["model"] == (security if facet == "security" else default)


def test_parallel_findings_reach_reranker_once_with_deterministic_lineage():
    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))
    graph = StateGraph(State)
    graph.add_node("implementation", lambda state: {"implementation": "Done"})
    calls = []

    for facet in module.REVIEW_FACETS:
        key = f"review-{facet.id}"
        node = nodes[key]

        async def review(state, node=node):
            await asyncio.sleep(0)
            return node._terminal_update(completed([{
                "tagline": node.facet, "description": "A concrete defect.",
                "agent": "spoofed", "facet": "spoofed",
            }]))
        graph.add_node(key, review)
        graph.add_edge(key, "reranker")

    def rerank(state):
        prompt = nodes["reranker"].prompt(state)
        assert "https://example.com/pr/1" in prompt
        findings = [item for f in module.REVIEW_FACETS for item in state[f"review-{f.id}"]]
        assert {f["facet"] for f in findings} == {f.id for f in module.REVIEW_FACETS}
        assert {f["agent"] for f in findings} == {"claude"}
        assert "spoofed" not in prompt
        calls.append(findings)
        return nodes["reranker"]._terminal_update(completed(findings[:1]))

    graph.add_node("reranker", rerank)
    graph.add_edge(START, "implementation")
    graph.add_conditional_edges("implementation", module._fan_out_reviews)
    graph.add_edge("reranker", END)
    result = asyncio.run(graph.compile().ainvoke({"pr_url": "https://example.com/pr/1"}))
    assert len(calls) == 1
    assert len(result["review"]) == 1
    assert "findings" not in result


def test_empty_results_and_reranker_lineage_requirement():
    review = ReviewNode(agent="claude", facet="bugs", cwd="/tmp", output_key="bugs")
    reranker = RerankerNode(agent="codex", cwd="/tmp", output_key="review")
    assert review._terminal_update(completed([])) == {"bugs": []}
    assert reranker._terminal_update(completed([])) == {"review": []}
    with pytest.raises(ValueError, match="lineage"):
        reranker._terminal_update(completed([{"tagline": "Bad", "description": "Bad"}]))
