"""Selects a workspace's provider, persists its ownership, and routes lifecycle
calls to whichever adapter owns it.

Provider selection is sticky: once a workspace is created with a provider, every
later call for it -- including recovery after this service restarts -- is routed
to that same adapter. An adapter that is not registered, or that refuses to
reassert ownership, fails the call explicitly; it is never substituted for a
different provider or a lower-isolation fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from virtual_control_plane.application.ports import ProviderUnavailable, UnsupportedProvider, WorkspaceProviderPort
from virtual_control_plane.persistence.store import Workspace, WorkspaceProvider, WorkspaceStatus, Workspaces

log = logging.getLogger(__name__)

ProviderRegistry = Mapping[WorkspaceProvider, WorkspaceProviderPort]


class WorkspaceService:
    def __init__(self, workspaces: Workspaces, providers: ProviderRegistry) -> None:
        self._workspaces = workspaces
        self._providers = providers

    def _provider(self, provider: WorkspaceProvider) -> WorkspaceProviderPort:
        try:
            return self._providers[provider]
        except KeyError:
            raise UnsupportedProvider(provider) from None

    async def create(self, provider: WorkspaceProvider) -> Workspace:
        """Provision a workspace with `provider` and persist its ownership.

        Raises `UnsupportedProvider` if no adapter is registered for it,
        rather than silently choosing a different, available one.
        """
        provisioned = await self._provider(provider).create()
        workspace = await self._workspaces.create(provider, provisioned.locator)
        return await self._workspaces.set_status(workspace.id, WorkspaceStatus.READY) or workspace

    async def recover(self, workspace_id: str) -> Workspace:
        """Reassert ownership of a persisted workspace with the provider that created it.

        This is what makes provider ownership survive a restart: the provider
        is read back from the persisted workspace, not re-chosen, and a
        provider that is unavailable or unsupported on this host raises
        rather than handing the workspace to another one.
        """
        workspace = await self._workspaces.get(workspace_id)
        if workspace is None:
            raise LookupError(workspace_id)
        try:
            provisioned = await self._provider(workspace.provider).recover(workspace.locator or {})
        except (ProviderUnavailable, UnsupportedProvider):
            log.warning(
                "%s's provider could not reassert ownership of %s; not falling back",
                workspace.provider.value, workspace_id,
                extra={"workspace.id": workspace_id, "workspace.provider": workspace.provider.value},
            )
            raise
        if provisioned.locator != workspace.locator:
            workspace = await self._workspaces.set_locator(workspace_id, provisioned.locator) or workspace
        return workspace

    async def destroy(self, workspace_id: str) -> None:
        workspace = await self._workspaces.get(workspace_id)
        if workspace is None:
            return
        await self._provider(workspace.provider).destroy(workspace.locator or {})
        await self._workspaces.set_status(workspace_id, WorkspaceStatus.STOPPED)
