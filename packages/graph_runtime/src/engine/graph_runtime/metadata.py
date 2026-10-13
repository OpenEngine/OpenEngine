"""Run identity and submission metadata, independent of checkpoint state."""
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from engine.domain import RunId, RunOrigin
from engine.graph_runtime.topology import GraphId


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMetadata:
    name: str | None = None
    prompt: str | None = None
    repository: str | None = None
    inputs: dict[str, str] | None = None
    requester: str | None = None
    origin_channel: str | None = None
    origin_thread_id: str | None = None
    origin: RunOrigin | None = None
    task_id: str | None = None
    parent_run_id: RunId | None = None
    depends_on_run_id: RunId | None = None
    scheduled: bool = False
    failure_reason: str | None = None
    created_at: str | None = None
    started_at: str | None = None


class MetadataRecord(Protocol):
    @property
    def run_id(self) -> RunId: ...
    @property
    def graph_id(self) -> GraphId: ...
    @property
    def metadata(self) -> RunMetadata: ...


class RunMetadataStore(Protocol):
    async def run(self, run_id: RunId) -> MetadataRecord | None: ...
    async def save_metadata(self, run_id: RunId, graph_id: GraphId,
                            metadata: RunMetadata, *, only_missing: bool = False) -> None: ...
    async def import_metadata(self, records: Sequence[tuple[RunId, GraphId, RunMetadata]]) -> None: ...
    async def list_runs(self) -> tuple[MetadataRecord, ...]: ...
    async def list_runs_for_origin(self, channel: str, thread_id: str) -> tuple[MetadataRecord, ...]: ...
    async def delete_run(self, run_id: RunId) -> bool: ...


def graph_run_name(values: dict[str, object]) -> str:
    """The concise name produced by a graph's naming node."""
    named = values.get("name")
    if isinstance(named, str) and named.strip():
        return named.strip().splitlines()[0].strip(" \t\"'`).:;!?")[:120]
    return ""


def run_name(metadata: RunMetadata, values: dict[str, object], run_id: RunId) -> str:
    """Use the graph's naming output when present, then submission metadata."""
    return graph_run_name(values) or metadata.name or metadata.prompt or str(run_id)
