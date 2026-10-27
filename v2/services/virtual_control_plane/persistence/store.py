"""The virtual control plane's store, as the rest of the service sees it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from openengine.persistence import Store


class WorkspaceProvider(StrEnum):
    PROCESS = "process"
    SMOLVM = "smolvm"
    KUBERNETES = "kubernetes"


class WorkspaceStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    STOPPED = "stopped"


Locator = dict[str, Any]
"""Opaque, provider-owned placement/ownership metadata; see the `locator` column."""


@dataclass(frozen=True)
class Workspace:
    id: str
    provider: WorkspaceProvider
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime
    locator: Locator | None = None


class Workspaces(Store, Protocol):
    async def create(self, provider: WorkspaceProvider, locator: Locator | None = None) -> Workspace:
        """A new workspace, provisioning until its provider reports it ready."""
        ...

    async def get(self, id: str) -> Workspace | None: ...

    async def set_status(self, id: str, status: WorkspaceStatus) -> Workspace | None: ...

    async def set_locator(self, id: str, locator: Locator) -> Workspace | None:
        """Record where a restart's provider now says the workspace lives."""
        ...

    async def list(self, status: WorkspaceStatus | None = None) -> list[Workspace]: ...
