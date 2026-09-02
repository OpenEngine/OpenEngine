"""A workflow variant, for tests that need a graph and not an agent.

The point of the variant mechanism, exercised: this directory is a second
workflow directory, and a deployment pointed at it runs *this* graph instead of
the repository's. Nothing is switched on to get here and no flag has two
settings to keep working -- the configuration names a directory, and the
directory decides.

Deliberately one node and no agent. What a graph *does* is tested against a real
ACP agent in `tests/test_graph_runtime_langgraph_acp.py`; what is being checked
against this file is composition, discovery and topology, and an agent here
would only mean a subprocess in tests that never needed one.

It is also the single-graph spelling of `graph_workflow`, where the repository's
own file is the family spelling -- so both forms are exercised by code that
someone actually loads.
"""

from engine.graph_runtime_langgraph import State, graph_workflow
from langgraph.graph import END, START, StateGraph


async def note(state: dict[str, object]) -> dict[str, object]:
    """Record that the run happened, and what it was asked to do."""
    return {"noted": state.get("task", "")}


note.graph_node_name = "Note"  # type: ignore[attr-defined]
note.graph_node_kind = "tool"  # type: ignore[attr-defined]
note.graph_node_description = "Writes the task down."  # type: ignore[attr-defined]


@graph_workflow(id="tiny", name="Tiny")
def workflow() -> StateGraph:
    builder: StateGraph = StateGraph(State)
    builder.add_node("note", note)
    builder.add_edge(START, "note")
    builder.add_edge("note", END)
    return builder
