"""Load trusted Python workflow modules into an immutable runtime catalog.

A repository's workflow directory says what this deployment can run. A module
there exports a `GraphWorkflow` -- or a sequence of them, so the same graph on
several runners can be one file rather than a file per variant that drifts.

`GraphWorkflow` is `engine.graph_runtime`'s protocol: an id and a name. So no
graph engine is imported here, and this module never learns what a graph is. It
loads the directory, refuses two workflows claiming the same id, and hands the
result to whoever has an engine to run them -- `apps/web` does.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from engine.graph_runtime import GraphWorkflow


class WorkflowLoadError(ValueError):
    """A configured workflow directory or definition is invalid."""


@dataclass(frozen=True, slots=True)
class WorkflowCatalog:
    """Workflows available for starting runs in this process.

    Iterating a catalog yields the graphs, in the order the directory declares
    them. `get`, `require`, `in` and `len` answer about the same set.
    """

    _graphs: Mapping[str, GraphWorkflow]

    @classmethod
    def from_graphs(cls, graphs: Iterable[GraphWorkflow]) -> "WorkflowCatalog":
        indexed: dict[str, GraphWorkflow] = {}
        for graph in graphs:
            identifier = str(graph.graph_id)
            if identifier in indexed:
                raise WorkflowLoadError(f"duplicate workflow id: {identifier}")
            indexed[identifier] = graph
        return cls(MappingProxyType(indexed))

    @property
    def graphs(self) -> tuple[GraphWorkflow, ...]:
        return tuple(self._graphs.values())

    def get(self, workflow_id: object) -> GraphWorkflow | None:
        return self._graphs.get(str(workflow_id))

    def require(self, workflow_id: object) -> GraphWorkflow:
        graph = self.get(workflow_id)
        if graph is None:
            raise WorkflowLoadError(f"unknown workflow definition: {workflow_id}")
        return graph

    def __contains__(self, workflow_id: object) -> bool:
        return str(workflow_id) in self._graphs

    def __iter__(self) -> Iterator[GraphWorkflow]:
        return iter(self._graphs.values())

    def __len__(self) -> int:
        return len(self._graphs)


def load_workflow_catalog(
    directory: str | Path,
    *,
    session_config: Mapping[str, object] | None = None,
) -> WorkflowCatalog:
    """Import sorted, non-private ``*.py`` definitions from one directory.

    When ``session_config`` is given, modules that export both ``graph_for``
    and ``RUNNERS`` are rebuilt with that config rather than using their
    pre-built ``workflow`` value. This lets a composition root wire deployment
    settings (attribution, output style) into ACP nodes without modifying the
    workflow definitions themselves.
    """

    root = Path(directory).resolve()
    if not root.is_dir():
        raise WorkflowLoadError(f"workflow directory does not exist: {root}")
    paths = sorted(path for path in root.glob("*.py") if not path.name.startswith("_"))
    if not paths:
        raise WorkflowLoadError(f"workflow directory contains no definitions: {root}")
    graphs: list[GraphWorkflow] = []
    sources: dict[str, Path] = {}
    for path in paths:
        module_name = "_openengine_workflow_" + sha256(str(path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise WorkflowLoadError(f"cannot import workflow definition: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            exported = _exported_with_config(module, path, session_config)
        except WorkflowLoadError:
            raise
        except Exception as error:
            raise WorkflowLoadError(f"{path}: {type(error).__name__}: {error}") from error
        finally:
            sys.modules.pop(module_name, None)
        for value in exported:
            identifier = str(value.graph_id)
            if identifier in sources:
                raise WorkflowLoadError(
                    f"{path}: duplicate workflow id {identifier}; "
                    f"first defined in {sources[identifier]}"
                )
            sources[identifier] = path
            graphs.append(value)
    return WorkflowCatalog.from_graphs(graphs)


def _exported(module: object, path: Path) -> tuple[GraphWorkflow, ...]:
    """What one module contributes: one workflow, or a family of variants."""
    try:
        exported = getattr(module, "workflow")
    except AttributeError as error:
        raise WorkflowLoadError(
            f"{path}: must export a value named 'workflow'"
        ) from error
    values = tuple(exported) if isinstance(exported, (list, tuple)) else (exported,)
    if not values:
        raise WorkflowLoadError(f"{path}: exported 'workflow' is empty")
    for value in values:
        if not isinstance(value, GraphWorkflow):
            raise WorkflowLoadError(
                f"{path}: exported 'workflow' is not a graph workflow"
            )
    return values


def _exported_with_config(
    module: object,
    path: Path,
    session_config: Mapping[str, object] | None,
) -> tuple[GraphWorkflow, ...]:
    """Like `_exported`, but rebuilds graph workflows with session config.

    When a module exports both ``graph_for`` (a callable that accepts
    ``session_config``) and ``RUNNERS`` (a sequence of runner names), and a
    non-``None`` session config was requested, the graph workflows are rebuilt
    through ``graph_for(runner, session_config=...)`` instead of reading the
    pre-built ``workflow`` value.
    """
    graph_for = getattr(module, "graph_for", None)
    runners = getattr(module, "RUNNERS", None)
    if (
        session_config is not None
        and callable(graph_for)
        and isinstance(runners, (list, tuple))
        and _accepts_session_config(graph_for)
    ):
        _exported(module, path)
        return tuple(
            graph_for(runner, session_config=session_config) for runner in runners
        )
    return _exported(module, path)


def _accepts_session_config(func: object) -> bool:
    """Whether ``func`` has a ``session_config`` keyword parameter."""
    try:
        signature = inspect.signature(func)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return "session_config" in signature.parameters


__all__ = ["WorkflowCatalog", "WorkflowLoadError", "load_workflow_catalog"]
