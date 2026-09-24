"""Per-node usage in approximate dollars, rolled up to its WorkOrder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from engine.domain import RunId
from engine.graph_runtime import EventKind, ExecutionId, NodeId, RuntimeEvent
from engine.graph_runtime.usage import TokenCounts, price_for, usage_rollup
from engine.graph_runtime_langgraph.acp import ACPNode, _Turn
from engine.graph_runtime_langgraph.executions import NodeExecution
from langgraph_acp import ACPEvent, ACPEventType

RUN = RunId("run")
IMPLEMENTATION = NodeId("implementation")
REVIEW = NodeId("review")


def usage(node: NodeId, **payload: object) -> RuntimeEvent:
    return RuntimeEvent(RUN, EventKind.USAGE_UPDATED, payload, node_id=node)


def turn(node: NodeId, session: str, agent: str = "claude", **tokens: int) -> RuntimeEvent:
    return usage(node, agent=agent, model="", sessionId=session, turn=True, **tokens)


def test_turns_are_priced_per_node_and_summed_to_the_workorder() -> None:
    rollup = usage_rollup([
        turn(IMPLEMENTATION, "a", agent="codex", inputTokens=1_000_000, outputTokens=100_000),
        turn(IMPLEMENTATION, "a", agent="codex", cachedReadTokens=1_000_000),
        turn(REVIEW, "b", inputTokens=200_000, outputTokens=20_000, cachedWriteTokens=40_000),
        RuntimeEvent(RUN, EventKind.TRANSCRIPT, {"text": "ignored"}, node_id=REVIEW),
    ])

    implementation = rollup.nodes[str(IMPLEMENTATION)]
    review = rollup.nodes[str(REVIEW)]
    # Codex: 1M input at $1.25, 100k output at $10, 1M cache reads at $0.125.
    assert implementation.cost_usd == pytest.approx(1.25 + 1.0 + 0.125)
    assert implementation.tokens.input_tokens == 1_000_000
    # Claude, priced as Opus: $1 input, $0.50 output, $0.25 cache writes.
    assert review.cost_usd == pytest.approx(1.75)
    assert rollup.total.cost_usd == pytest.approx(2.375 + 1.75)
    assert rollup.total.estimated and rollup.total.complete
    body = rollup.json()
    assert body["costUsd"] == pytest.approx(4.125)
    assert set(body["nodes"]) == {"implementation", "review"}  # type: ignore[arg-type]


def test_reported_session_cost_replaces_rather_than_adds() -> None:
    rollup = usage_rollup([
        usage(IMPLEMENTATION, agent="claude", sessionId="a", sessionCostUsd=0.01),
        turn(IMPLEMENTATION, "a", inputTokens=100),
        usage(IMPLEMENTATION, agent="claude", sessionId="a", sessionCostUsd=0.016),
        # A second conversation in the same node is a second session to add.
        usage(IMPLEMENTATION, agent="claude", sessionId="b", sessionCostUsd=0.5),
    ])

    node = rollup.nodes[str(IMPLEMENTATION)]
    assert node.cost_usd == pytest.approx(0.516)
    assert not node.estimated
    assert node.tokens.input_tokens == 100


def test_an_unpriced_agent_is_unknown_not_free() -> None:
    rollup = usage_rollup([
        turn(IMPLEMENTATION, "a", agent="homegrown", inputTokens=10),
        turn(REVIEW, "b", inputTokens=1_000_000),
    ])

    assert rollup.nodes[str(IMPLEMENTATION)].cost_usd is None
    assert rollup.total.cost_usd == pytest.approx(5.0)
    assert not rollup.total.complete
    assert usage_rollup([]).json()["costUsd"] is None


def test_non_finite_numbers_are_ignored() -> None:
    inf, nan = float("inf"), float("nan")
    rollup = usage_rollup([
        usage(IMPLEMENTATION, agent="claude", sessionId="a", sessionCostUsd=0.01),
        usage(IMPLEMENTATION, agent="claude", sessionId="a", sessionCostUsd=inf),
        usage(IMPLEMENTATION, agent="claude", sessionId="a", sessionCostUsd=nan),
        turn(IMPLEMENTATION, "a", inputTokens=inf, outputTokens=nan, cachedReadTokens=5),
    ])

    node = rollup.nodes[str(IMPLEMENTATION)]
    assert node.cost_usd == pytest.approx(0.01)
    assert node.tokens == TokenCounts(cached_read_tokens=5)


def test_the_configured_model_is_priced_before_the_agent() -> None:
    assert price_for("claude", "claude-haiku-4-5") == price_for("haiku")
    assert price_for("claude") == price_for("claude", "opus")
    assert price_for("stub") is None


def test_acp_node_publishes_turn_tokens_and_session_cost() -> None:
    async def scenario() -> list[Any]:
        class Session:
            async def prompt(self, prompt: Any) -> AsyncIterator[ACPEvent]:
                yield ACPEvent(agent="claude", type=ACPEventType.USAGE_UPDATED, session_id="s",
                               data={"used": 10, "cost": {"amount": 0.02, "currency": "USD"}})
                yield ACPEvent(agent="claude", type=ACPEventType.USAGE_UPDATED, session_id="s",
                               data={"cost": {"amount": 3, "currency": "EUR"}})
                yield ACPEvent(agent="claude", type=ACPEventType.USAGE_UPDATED, session_id="s",
                               data={"cost": {"amount": float("inf"), "currency": "USD"}})
                yield ACPEvent(agent="claude", type=ACPEventType.PROMPT_COMPLETED, session_id="s",
                               data={"stopReason": "end_turn",
                                     "usage": {"inputTokens": 7, "outputTokens": 3,
                                               "cachedReadTokens": float("nan")}})

            async def cancel(self) -> None: ...

        runtime = SimpleNamespace(publish=AsyncMock())
        execution = NodeExecution(runtime, RUN, ExecutionId("execution"), IMPLEMENTATION)
        node = ACPNode(agent="claude", cwd="/tmp", session_config={"model": "sonnet"})
        await node._speak(_Turn(node, execution, "session"), Session(), "go")
        return [
            call.args[2] for call in runtime.publish.await_args_list
            if call.args[1] is EventKind.USAGE_UPDATED
        ]

    published = asyncio.run(scenario())
    assert published == [
        {"agent": "claude", "model": "sonnet", "sessionId": "s", "sessionCostUsd": 0.02},
        {"agent": "claude", "model": "sonnet", "sessionId": "s", "turn": True,
         "inputTokens": 7, "outputTokens": 3},
    ]
