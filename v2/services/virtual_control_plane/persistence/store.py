"""The virtual control plane's store, as the rest of the service sees it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from openengine.persistence import Store


class WorkspaceProvider(StrEnum):
    PROCESS = "process"
    SMOLVM = "smolvm"
    KUBERNETES = "kubernetes"


class WorkspaceStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Workspace:
    id: str
    provider: WorkspaceProvider
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime


class Workspaces(Store, Protocol):
    async def create(self, provider: WorkspaceProvider) -> Workspace:
        """A new workspace, provisioning until its provider reports it ready."""
        ...

    async def get(self, id: str) -> Workspace | None: ...

    async def set_status(self, id: str, status: WorkspaceStatus) -> Workspace | None: ...

    async def list(self, status: WorkspaceStatus | None = None) -> list[Workspace]: ...
