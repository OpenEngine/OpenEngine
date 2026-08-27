"""A complete ACPNode invocation against a stubbed ACP CLI."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from langgraph_acp import ACPAgentRegistry, ACPNode, ACPResult, StdioACPProvider

FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"


def sent_methods(log: Path) -> list[str]:
    messages: list[dict[str, Any]] = [
        json.loads(line) for line in log.read_text().splitlines()
    ]
    return [message["method"] for message in messages if "method" in message]


def test_a_node_runs_a_complete_turn_against_a_stubbed_cli(tmp_path: Path) -> None:
    """The first meaningful path: graph input in, normalized result out."""
    log = tmp_path / "sent.jsonl"
    registry = ACPAgentRegistry(
        [
            StdioACPProvider(
                name="codex",
                command=(sys.executable, str(FAKE_AGENT)),
                env={"FAKE_AGENT_LOG": str(log)},
            )
        ]
    )
    node = ACPNode(agent="codex", registry=registry)

    result = asyncio.run(node("Review this change"))

    assert isinstance(result, ACPResult)
    assert result == ACPResult(
        message="Looking.",
        content=({"type": "text", "text": "Looking."},),
        agent="codex",
        session_id="sess_fake_1",
        stop_reason="end_turn",
    )
    assert sent_methods(log) == ["initialize", "session/new", "session/prompt"]


def test_the_node_sends_its_invocation_input_as_the_prompt(tmp_path: Path) -> None:
    log = tmp_path / "sent.jsonl"
    registry = ACPAgentRegistry(
        [
            StdioACPProvider(
                name="codex",
                command=(sys.executable, str(FAKE_AGENT)),
                env={"FAKE_AGENT_LOG": str(log)},
            )
        ]
    )

    asyncio.run(ACPNode(agent="codex", registry=registry)("Review ticket 4"))

    messages = [json.loads(line) for line in log.read_text().splitlines()]
    prompt = next(
        message["params"]["prompt"]
        for message in messages
        if message.get("method") == "session/prompt"
    )
    assert prompt == [{"type": "text", "text": "Review ticket 4"}]
