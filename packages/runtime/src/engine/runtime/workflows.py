"""Load trusted Python workflow modules into an immutable runtime catalog."""

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


class WorkflowLoadError(ValueError):
    """A configured workflow directory or definition is invalid."""


@dataclass(frozen=True, slots=True)
class WorkflowCatalog:
    """Definitions available for starting runs in this process."""

    _definitions: Mapping[WorkflowId, WorkflowDefinition]

    @classmethod
    def from_definitions(
        cls, definitions: Iterable[WorkflowDefinition]
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
        return cls(MappingProxyType(indexed))

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
    sources: dict[WorkflowId, Path] = {}
    for path in paths:
        module_name = "_openengine_workflow_" + sha256(str(path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise WorkflowLoadError(f"cannot import workflow definition: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            definition = getattr(module, "workflow")
            if not isinstance(definition, WorkflowDefinition):
                raise WorkflowLoadError(
                    f"{path}: exported 'workflow' is not an openengine workflow"
                )
            openengine.validate(definition)
        except WorkflowLoadError:
            raise
        except AttributeError as error:
            raise WorkflowLoadError(
                f"{path}: must export a value named 'workflow'"
            ) from error
        except Exception as error:
            raise WorkflowLoadError(f"{path}: {type(error).__name__}: {error}") from error
        finally:
            sys.modules.pop(module_name, None)
        if definition.workflow_id in sources:
            raise WorkflowLoadError(
                f"{path}: duplicate workflow id {definition.workflow_id}; "
                f"first defined in {sources[definition.workflow_id]}"
            )
        sources[definition.workflow_id] = path
        definitions.append(definition)
    return WorkflowCatalog.from_definitions(iter(definitions))


__all__ = ["WorkflowCatalog", "WorkflowLoadError", "load_workflow_catalog"]
