"""Stop, and wait for a person, without interrupting the graph.

A run that has to be decided by somebody is what an approval already is, so this
asks one and waits on it rather than ending the task and being re-entered. The
difference matters after a crash: `execution.ask` writes the question down
*before* it suspends, so a process that dies here leaves a question somebody can
still answer, and answering it picks the run back up where it stopped.

The note a person writes beside their decision arrives as **steering**, not as a
field on the decision. Steering is the channel for saying something to an
execution that is already running, and the decision endpoint deliberately
carries a decision and nothing else -- so a client sends the words first and the
verdict second, and this node reads whatever arrived while it was waiting.

    POST .../steering    "ship it, the finding can wait"
    POST .../approvals   accept
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from engine.domain import ApprovalDecision, ApprovalKind
from engine.graph_runtime import EventKind
from engine.graph_runtime_langgraph.executions import current_execution

#: The tool name the request is raised under, so a client can tell the one
#: approval that is a person's verdict from the ones that are an agent asking
#: permission to do something.
TOOL_NAME = "human_review"

#: The state key the verdict lands in: "approved" or "rejected".
DECISION = "decision"

#: The state key the note lands in.
NOTE = "decisionNote"


@dataclass(frozen=True, slots=True)
class HumanReviewNode:
    """Raise an approval, wait for it, and record what was decided."""

    reason: str = "approval of this run"
    """What is being asked, worded so the runtime's refusal -- "<reason> was not
    allowed" -- is a sentence."""
    prompt: str = (
        "The implementation and the review are done. A person decides whether "
        "this run is accepted."
    )

    graph_node_name: str = "Human review"
    graph_node_kind: str = "human"
    graph_node_description: str = "A person accepts or rejects the run."
    graph_node_show_in_sidebar: bool = False
    """Not one of the run's conversations, so it is not offered as one.

    What happens here is the reader's own decision, and it is presented where
    the decision is made -- the WorkOrder page, beside what it is made from.
    A rail entry leading to a transcript of somebody's own verdict would be a
    conversation with nobody in it.
    """

    async def __call__(self, _state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        await execution.say(self.prompt)
        decision = await execution.ask(
            reason=self.reason,
            kind=ApprovalKind.USER_INPUT,
            tool_name=TOOL_NAME,
        )
        note = "\n".join(execution.pending_messages()).strip()
        accepted = decision is not ApprovalDecision.CANCEL
        if note:
            await execution.say(note, role="user")
        await execution.emit(
            EventKind.TRANSCRIPT,
            {
                "role": "assistant",
                "text": f"Recorded: {'approved' if accepted else 'rejected'}.",
            },
        )
        return {DECISION: "approved" if accepted else "rejected", NOTE: note}


__all__ = ["DECISION", "NOTE", "TOOL_NAME", "HumanReviewNode"]
