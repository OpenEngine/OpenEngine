"""Best-effort WorkOrder naming as a reusable graph node."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from langgraph_acp import ACPPrompt

from engine.graph_runtime_langgraph.acp import ACPNode
from engine.graph_runtime_langgraph.executions import current_execution

log = logging.getLogger(__name__)

NAMING_PROMPT = (
    "Give this WorkOrder a concise display name based on the task below. When "
    "the request points at an issue or a pull request instead of describing the "
    "work, read that item first and name what it is actually about. If it names "
    "an issue or pull request by number, lead the name with the number, as in "
    '"#270 Dependencies can run arbitrary install scripts". Do not change the '
    "workspace and do not perform the task. Return a JSON object with a single "
    '"name" field, for example {{"name": "#270 Secure dependency installs"}}. '
    "The name must be a non-empty string of at most twelve words, with no "
    "ending punctuation. Return no Markdown fences or text after the JSON.\n\n"
    "The task:\n{task}"
)


def naming_prompt(state: Mapping[str, object]) -> str:
    """Build the standard naming request from a graph's task."""
    return NAMING_PROMPT.format(task=state.get("task", ""))


@dataclass(frozen=True, slots=True, kw_only=True)
class NameNode(ACPNode):
    """Ask an ACP agent for optional display metadata without risking the work.

    Naming is deliberately not a conversation in the sidebar and deliberately
    cannot fail the graph. A provider or tool failure leaves the WorkOrder with
    its existing fallback name and lets the next node run. Cancellation is
    different: it belongs to the execution driving the graph and must propagate.
    """

    prompt: str | Callable[[Mapping[str, object]], ACPPrompt] = naming_prompt
    output_key: str = "name"
    graph_node_name: str = "Naming"
    graph_node_description: str = "Gives the WorkOrder a concise display name."
    graph_node_show_in_sidebar: bool = False

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        try:
            update = await ACPNode.__call__(self, state)
            key = self.output_key or str(current_execution().node_id)
            response = update[key]
            if isinstance(response, str):
                # ACP includes narration before tool calls in the turn's text.
                # Only a final JSON object may supply the display name.
                for index, character in enumerate(response):
                    if character != "{":
                        continue
                    try:
                        value = json.loads(response[index:])
                    except json.JSONDecodeError:
                        continue
                    name = value.get("name")
                    if isinstance(name, str) and name.strip():
                        return {key: name.strip()}
                    break
            raise ValueError(
                "naming response must end with a JSON object containing a name"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "could not name graph run %s", current_execution().run_id
            )
            return {}


__all__ = ["NameNode"]
