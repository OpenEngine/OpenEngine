"""Catalog-driven run read model.

A WorkOrder row plus the name of the workflow it ran. Progress inside the run
belongs to the graph engine, which the interface asks separately -- so what is
built here is identity, submission metadata and the lifecycle projection, and
nothing that would have to be kept in step with a running graph.
"""

from dataclasses import dataclass
from collections.abc import Sequence
from datetime import UTC, datetime

from engine.domain import (
    RunId,
    RunPhase,
    RunState,
    TaskId,
    WorkflowId,
)
from engine.graph_runtime import GraphRuntime, RunStatus, UnknownGraphError, UnknownRunError
from engine.graph_runtime.topology import GraphId
from engine.graph_runtime.metadata import RunMetadata, RunMetadataStore, MetadataRecord, graph_run_name
from engine.runtime.workflows import WorkflowCatalog


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    run_id: RunId
    name: str
    workflow_id: str
    workflow_name: str
    task_id: str
    task_prompt: str
    repository: str
    phase: str
    terminal_outcome: str | None
    failure_reason: str = ""
    parent_run_id: RunId | None = None
    depends_on_run_id: RunId | None = None
    requester: str | None = None
    started_at: datetime | None = None


class RunReader:
    """Build run views from durable state and the catalog's workflow names."""

    def __init__(self, catalog: WorkflowCatalog | None = None) -> None:
        self.store: RunMetadataStore | None = None
        self.runtime: GraphRuntime | None = None
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
        return tuple(self._view(state) for state in await self.list_runs())

    async def get(self, run_id: RunId) -> WorkflowRunView | None:
        state = await self.load(run_id)
        return self._view(state) if state is not None else None

    async def load(self, run_id: RunId) -> RunState | None:
        record = await self.store.run(run_id) if self.store else None
        return await self._state(record) if record else None

    async def list_runs(self) -> tuple[RunState, ...]:
        if self.store is None:
            return ()
        return tuple([await self._state(row) for row in await self.store.list_runs()])

    async def dependency_candidates(self) -> tuple[RunState, ...]:
        if self.store is None:
            return ()
        rows = await self.store.list_runs()
        scheduled = {row.run_id for row in rows if row.metadata.scheduled}
        # A scheduled prerequisite cannot have succeeded. Filter on metadata
        # before building views, so a long chain does not rebuild every waiting
        # run and its prerequisite each time one run completes. The dispatcher
        # still checks the remaining prerequisites' runtime phases.
        return tuple([
            await self._state(row) for row in rows
            if row.metadata.scheduled
            and row.metadata.depends_on_run_id is not None
            and row.metadata.depends_on_run_id not in scheduled
        ])

    async def list_runs_for_origin(self, channel: str, thread_id: str) -> tuple[RunState, ...]:
        return tuple([await self._state(row) for row in await self.store.list_runs_for_origin(channel, thread_id)]) if self.store else ()

    async def delete_run(self, run_id: RunId) -> bool:
        return await self.store.delete_run(run_id) if self.store else False

    async def save(self, state: RunState, *, only_missing: bool = False) -> None:
        if self.store is None:
            message = "graph run store is unavailable"
            raise RuntimeError(message)
        old = await self.store.run(state.run_id)
        metadata = RunMetadata(
            name=state.name, prompt=state.prompt, repository=state.repository,
            inputs=state.inputs, requester=state.requester, origin=state.origin,
            origin_channel=state.origin.channel if state.origin else None,
            origin_thread_id=state.origin.thread_id if state.origin else None,
            task_id=str(state.task_id), parent_run_id=state.parent_run_id,
            depends_on_run_id=state.depends_on_run_id, scheduled=state.phase is RunPhase.SCHEDULED,
            failure_reason=state.failure_reason,
            created_at=old.metadata.created_at if old and old.metadata.created_at else datetime.now(UTC).isoformat(),
            started_at=state.started_at.isoformat() if state.started_at else None,
        )
        await self.store.save_metadata(state.run_id, GraphId(str(state.workflow_id)), metadata, only_missing=only_missing)

    async def backfill(self, states: Sequence[RunState]) -> None:
        for state in states:
            await self.save(state, only_missing=True)

    async def _state(self, record: MetadataRecord) -> RunState:
        metadata = record.metadata
        phase = RunPhase.SCHEDULED if metadata.scheduled else RunPhase.PENDING
        reason = metadata.failure_reason or ""
        name = metadata.name or ""
        if not metadata.scheduled and self.runtime is not None:
            try:
                snapshot = await self.runtime.snapshot(record.run_id)
            except UnknownGraphError:
                snapshot = None
                reason = reason or f"the workflow this WorkOrder ran, {record.graph_id}, is no longer available"
            except UnknownRunError:
                snapshot = None
            if snapshot is None:
                phase = RunPhase.FAILED
                reason = reason or "the graph engine has no record of this run"
            else:
                phase = {
                    RunStatus.SCHEDULED: RunPhase.SCHEDULED,
                    RunStatus.RUNNING: RunPhase.RUNNING_AGENT,
                    RunStatus.AWAITING_APPROVAL: RunPhase.RUNNING_AGENT,
                    RunStatus.COMPLETED: RunPhase.SUCCEEDED,
                    RunStatus.FAILED: RunPhase.FAILED,
                }[snapshot.status]
                reason = snapshot.error or reason
                values = snapshot.values or {}
                name = graph_run_name(dict(values)) or name
        if reason:
            phase = RunPhase.FAILED
        return RunState(
            run_id=record.run_id, task_id=TaskId(metadata.task_id or str(record.run_id)),
            workflow_id=WorkflowId(str(record.graph_id)), phase=phase, name=name,
            prompt=metadata.prompt or "", repository=metadata.repository or "",
            inputs=metadata.inputs or {}, requester=metadata.requester,
            origin=metadata.origin, parent_run_id=metadata.parent_run_id,
            depends_on_run_id=metadata.depends_on_run_id, failure_reason=reason,
            started_at=datetime.fromisoformat(metadata.started_at) if metadata.started_at else None,
        )

    def _view(self, state: RunState) -> WorkflowRunView:
        return WorkflowRunView(
            run_id=state.run_id,
            name=state.name or state.prompt or str(state.run_id),
            workflow_id=str(state.workflow_id),
            workflow_name=self._names.get(
                str(state.workflow_id), str(state.workflow_id)
            ),
            task_id=str(state.task_id),
            task_prompt=state.prompt,
            repository=state.repository,
            phase=state.phase.value,
            terminal_outcome=_terminal_outcome(state),
            failure_reason=state.failure_reason,
            parent_run_id=state.parent_run_id,
            depends_on_run_id=state.depends_on_run_id,
            requester=state.requester,
            started_at=state.started_at,
        )


def _terminal_outcome(state: RunState) -> str | None:
    if state.phase is RunPhase.FAILED:
        return "failed"
    if state.phase is RunPhase.SUCCEEDED:
        return "succeeded"
    return None


__all__ = ["RunReader", "WorkflowRunView"]
