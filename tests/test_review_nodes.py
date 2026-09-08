"""Finding contracts and a real LangGraph fanout/join using scripted agent turns."""

import asyncio
import json

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from engine.graph_runtime_langgraph import State
from engine.graph_runtime_langgraph.acp import ACPNode
from engine.graph_runtime_langgraph.components.review import (
    Finding,
    ReviewNode,
    RerankerNode,
    PublishReviewNode,
    finding_comment,
)

CANDIDATE = dict(
    tagline="Private records can be read by anyone",
    description="The new endpoint skips the permission check.",
    file="api.py",
    line=12,
    evidence="GET /records without credentials returns 200",
)


def test_parallel_findings_survive_join_and_only_selected_findings_are_published(
    monkeypatch,
):
    finished = set()

    async def turn(self, state):
        if isinstance(self, ReviewNode):
            await asyncio.sleep(0)
            finished.add(self.output_key)
            return {self.output_key: json.dumps({"findings": [CANDIDATE]})}
        if isinstance(self, RerankerNode):
            assert finished == {"security", "bugs", "performance", "conciseness"}
            assert all(len(state[key]) == 1 for key in finished)
            return {
                self.output_key: json.dumps(
                    {
                        "findings": [
                            {
                                "id": "security:1",
                                "reason": "Confirmed unauthenticated access",
                            }
                        ]
                    }
                )
            }
        prompt = self._prompt(state)
        assert "engine-finding:security:1" in prompt
        assert "engine-finding:bugs:1" not in prompt
        assert "Confirmed unauthenticated access" in prompt
        return {self.output_key: '{"pr_url":"https://github.com/o/r/pull/1"}'}

    monkeypatch.setattr(ACPNode, "__call__", turn)
    builder = StateGraph(State)
    keys = ("security", "bugs", "performance", "conciseness")
    for key in keys:
        builder.add_node(
            key,
            ReviewNode(
                agent="claude", model="opus", cwd="/tmp", facet=key, output_key=key
            ),
        )
        builder.add_edge(START, key)
    builder.add_node(
        "rerank",
        RerankerNode(agent="claude", cwd="/tmp", review_keys=keys, output_key="rerank"),
    )
    builder.add_edge(list(keys), "rerank")
    builder.add_node(
        "publish",
        PublishReviewNode(
            agent="codex", cwd="/tmp", findings_key="rerank", output_key="publish"
        ),
    )
    builder.add_edge("rerank", "publish")
    builder.add_edge("publish", END)
    state = asyncio.run(builder.compile().ainvoke({"task": "Add records API"}))
    assert state["pr_url"] == "https://github.com/o/r/pull/1"
    finding = Finding.model_validate(state["rerank"][0])
    assert finding.agent == "claude"
    assert finding.reviewer_node == "security"
    assert finding.reranker_node == "rerank"
    assert "opus" in finding_comment(finding)


@pytest.mark.parametrize(
    "answer",
    [
        "{}",
        '{"findings":"none"}',
        "No issues",
        json.dumps({"findings": [{**CANDIDATE, "line": 0}]}),
        json.dumps({"findings": [{**CANDIDATE, "tagline": "a\nb\nc"}]}),
        json.dumps({"findings": [{**CANDIDATE, "agent": "forged"}]}),
    ],
)
def test_malformed_review_does_not_silently_become_clean(monkeypatch, answer):
    async def turn(self, state):
        return {self.output_key: answer}

    monkeypatch.setattr(ACPNode, "__call__", turn)
    node = ReviewNode(agent="claude", cwd="/tmp", facet="security", output_key="review")
    with pytest.raises(ValidationError):
        asyncio.run(node({}))


@pytest.mark.parametrize("selections", [[], [{"id": "invented", "reason": "bad"}]])
def test_reranker_empty_and_unknown_findings(monkeypatch, selections):
    async def turn(self, state):
        return {self.output_key: json.dumps({"findings": selections})}

    monkeypatch.setattr(ACPNode, "__call__", turn)
    node = RerankerNode(
        agent="claude", cwd="/tmp", review_keys=("review",), output_key="rank"
    )
    if selections:
        with pytest.raises(ValueError, match="Unknown"):
            asyncio.run(node({"review": []}))
    else:
        assert asyncio.run(node({"review": []})) == {"rank": []}


def test_model_selection_uses_advertised_id_and_rejects_missing_models():
    from langgraph_acp._stdio import StdioACPSession

    class Client:
        calls = []

        async def call(self, method, params):
            self.calls.append((method, params))
            return {"configOptions": options}

    options = [
        {
            "id": "model-selector",
            "category": "model",
            "options": [{"value": "claude-opus-4-6", "name": "Claude Opus 4.6"}],
        }
    ]
    client = Client()
    session = StdioACPSession(client, "s1", options)
    asyncio.run(session.set_model("opus"))
    assert client.calls == [
        (
            "session/set_config_option",
            {
                "sessionId": "s1",
                "configId": "model-selector",
                "value": "claude-opus-4-6",
            },
        )
    ]
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(session.set_model("sonnet"))
    with pytest.raises(ValueError, match="advertise"):
        asyncio.run(StdioACPSession(client, "s2").set_model("opus"))
