"""Load trusted Python workflow modules into an immutable runtime catalog.

A repository's workflow directory says what this deployment can run. A module
there is classified by what it exports:

    openengine.workflow(...)   steps      -> the workflow runtime
    a `GraphWorkflow`          a graph    -> a graph runtime

Recognising a graph workflow is all this module does with one. It is loaded,
checked for an id nothing else claims, and set aside in `graphs`; nothing here
runs or serves it. An app that has a graph engine -- `apps/web` -- reads that
list and runs them, and one that has not simply ignores it, rather than the
directory refusing to load and taking the deployment down.

`GraphWorkflow` is `engine.graph_runtime`'s protocol: an id and a name. So no
graph engine is imported here, and this module never learns what a graph is.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import openengine
from engine.domain import WorkflowDefinition, WorkflowId
from engine.graph_runtime import GraphWorkflow


class WorkflowLoadError(ValueError):
    """A configured workflow directory or definition is invalid."""


@dataclass(frozen=True, slots=True)
class WorkflowCatalog:
    """Definitions available for starting runs in this process.

    Iterating a catalog yields step definitions and only those, because that is
    what every caller means by "the workflows": the ones that can be started.
    `get`, `require`, `in` and `len` answer about the same set.

    That is also what keeps a graph workflow away from the step executor: it
    iterates a catalog to find what to run, and a graph is not something it
    could run. An interface that offers both reads both -- `graphs` beside this
    iteration -- and says which is which.

    A catalog holding nothing but graphs is therefore falsy. Ask what you mean:
    `catalog is not None`, or `catalog.graphs`.
    """

    _definitions: Mapping[WorkflowId, WorkflowDefinition]
    graphs: tuple[GraphWorkflow, ...] = ()
    """Workflows that run as a graph, in the order the directory declares them.

    Set aside rather than mixed in: a graph has no steps, so anything that
    iterates this catalog to run something must never be handed one. Whoever
    can run a graph asks for them here by name -- `apps/web` does, and offers
    them in its dropdown behind a `[BETA]` label.
    """

    @classmethod
    def from_definitions(
        cls,
        definitions: Iterable[WorkflowDefinition],
        graphs: Iterable[GraphWorkflow] = (),
    ) -> "WorkflowCatalog":
        indexed: dict[WorkflowId, WorkflowDefinition] = {}
        for definition in definitions:
            try:
                openengine.validate(definition)
            except (TypeError, ValueError) as error:
                raise WorkflowLoadError(str(error)) from error
            if definition.workflow_id in indexed:
                raise WorkflowLoadError(
                    f"duplicate workflow id: {definition.workflow_id}"
                )
            indexed[definition.workflow_id] = definition
        return cls(MappingProxyType(indexed), _unique_graphs(graphs, indexed))

    def get(self, workflow_id: WorkflowId) -> WorkflowDefinition | None:
        return self._definitions.get(workflow_id)

    def require(self, workflow_id: WorkflowId) -> WorkflowDefinition:
        definition = self.get(workflow_id)
        if definition is None:
            raise WorkflowLoadError(f"unknown workflow definition: {workflow_id}")
        return definition

    def __contains__(self, workflow_id: object) -> bool:
        return workflow_id in self._definitions

    def __iter__(self) -> Iterator[WorkflowDefinition]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)


def load_workflow_catalog(directory: str | Path) -> WorkflowCatalog:
    """Import sorted, non-private ``*.py`` definitions from one directory."""

    root = Path(directory).resolve()
    if not root.is_dir():
        raise WorkflowLoadError(f"workflow directory does not exist: {root}")
    paths = sorted(path for path in root.glob("*.py") if not path.name.startswith("_"))
    if not paths:
        raise WorkflowLoadError(f"workflow directory contains no definitions: {root}")
    definitions: list[WorkflowDefinition] = []
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
            exported = _exported(module, path)
        except WorkflowLoadError:
            raise
        except Exception as error:
            raise WorkflowLoadError(f"{path}: {type(error).__name__}: {error}") from error
        finally:
            sys.modules.pop(module_name, None)
        for value in exported:
            identifier = (
                str(value.workflow_id)
                if isinstance(value, WorkflowDefinition)
                else str(value.graph_id)
            )
            if identifier in sources:
                raise WorkflowLoadError(
                    f"{path}: duplicate workflow id {identifier}; "
                    f"first defined in {sources[identifier]}"
                )
            sources[identifier] = path
            if isinstance(value, WorkflowDefinition):
                definitions.append(value)
            else:
                graphs.append(value)
    return WorkflowCatalog.from_definitions(iter(definitions), graphs)


def _exported(
    module: object, path: Path
) -> tuple[WorkflowDefinition | GraphWorkflow, ...]:
    """What one module contributes: one workflow, or a family of variants.

    A sequence is allowed so that the same graph on several agents can be one
    file. A file per variant would mean the body copied per variant, and copies
    drift.
    """
    try:
        exported = getattr(module, "workflow")
    except AttributeError as error:
        raise WorkflowLoadError(
            f"{path}: must export a value named 'workflow'"
        ) from error
    values = tuple(exported) if isinstance(exported, (list, tuple)) else (exported,)
    if not values:
        raise WorkflowLoadError(f"{path}: exported 'workflow' is empty")
    checked: list[WorkflowDefinition | GraphWorkflow] = []
    for value in values:
        if isinstance(value, WorkflowDefinition):
            openengine.validate(value)
        elif not isinstance(value, GraphWorkflow):
            raise WorkflowLoadError(
                f"{path}: exported 'workflow' is neither an openengine workflow "
                "nor a graph workflow"
            )
        checked.append(value)
    return tuple(checked)


def _unique_graphs(
    graphs: Iterable[GraphWorkflow],
    definitions: Mapping[WorkflowId, WorkflowDefinition],
) -> tuple[GraphWorkflow, ...]:
    """Graph ids, checked against each other and against the step workflows.

    One namespace across both kinds, because somebody picking something to run
    is choosing from one list and does not care which engine is behind it.
    """
    seen: set[str] = {str(workflow_id) for workflow_id in definitions}
    ordered: list[GraphWorkflow] = []
    for graph in graphs:
        identifier = str(graph.graph_id)
        if identifier in seen:
            raise WorkflowLoadError(f"duplicate workflow id: {identifier}")
        seen.add(identifier)
        ordered.append(graph)
    return tuple(ordered)


__all__ = ["WorkflowCatalog", "WorkflowLoadError", "load_workflow_catalog"]
