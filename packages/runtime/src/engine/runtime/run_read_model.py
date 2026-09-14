"""Catalog-driven run read model.

A WorkOrder row plus the name of the workflow it ran. Progress inside the run
belongs to the graph engine, which the interface asks separately -- so what is
built here is identity, submission metadata and the lifecycle projection, and
nothing that would have to be kept in step with a running graph.
"""

from dataclasses import dataclass

from engine.domain import (
    MilestoneId,
    RunId,
    RunPhase,
    RunState,
)
from engine.ports.state_store import StateStore
from engine.runtime.workflows import WorkflowCatalog


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    run_id: RunId
    name: str
    workflow_id: str
    workflow_name: str
    task_id: str
    milestone_id: MilestoneId | None
    task_prompt: str
    repository: str
    phase: str
    terminal_outcome: str | None
    failure_reason: str = ""


class RunReader:
    """Build run views from durable state and the catalog's workflow names."""

    def __init__(self, store: StateStore, catalog: WorkflowCatalog | None = None) -> None:
        self._store = store
        self._catalog = catalog if catalog is not None else WorkflowCatalog.from_graphs(())
        # A run's row carries the workflow id it was started with and nothing
        # else about the workflow, so without this its rows would be labelled
        # with that id -- the sort of thing that makes a list look broken.
        #
        # Ids a graph has retired are named here too, so a WorkOrder started
        # before its workflow was renamed reads as the workflow it ran rather
        # than as the id nothing answers to any more.
        self._names = {
            str(identifier): graph.name
            for graph in self._catalog.graphs
            for identifier in (
                *getattr(graph, "previous_ids", ()),
                graph.graph_id,
            )
        }

    async def list(self) -> tuple[WorkflowRunView, ...]:
        return tuple(self._view(state) for state in await self._store.list_runs())

    async def get(self, run_id: RunId) -> WorkflowRunView | None:
        state = await self._store.load(run_id)
        return self._view(state) if state is not None else None

    def _view(self, state: RunState) -> WorkflowRunView:
        return WorkflowRunView(
            run_id=state.run_id,
            name=state.name or state.prompt or str(state.run_id),
            workflow_id=str(state.workflow_id),
            workflow_name=self._names.get(
                str(state.workflow_id), str(state.workflow_id)
            ),
            task_id=str(state.task_id),
            milestone_id=state.milestone_id,
            task_prompt=state.prompt,
            repository=state.repository,
            phase=state.phase.value,
            terminal_outcome=_terminal_outcome(state),
            failure_reason=state.failure_reason,
        )


def _terminal_outcome(state: RunState) -> str | None:
    if state.phase is RunPhase.FAILED:
        return "failed"
    if state.phase is RunPhase.SUCCEEDED:
        return "succeeded"
    return None


__all__ = ["RunReader", "WorkflowRunView"]
