"""Shared lifecycle conformance: the same assertions against a worktree-shaped
and a VM-shaped `WorkspaceProviderPort` double.

#1054 (worktree) and #1114 (smolvm) are expected to pass this same module
against their real adapters; `WorkspaceService` never branches on which
provider it holds, so neither should they need a provider-specific test file
for this lifecycle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openengine.persistence import DatabaseConfig

from virtual_control_plane.application.workspaces import WorkspaceService
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.store import WorkspaceStatus
from virtual_control_plane.tests.fakes import FakeVmProvider, FakeWorktreeProvider

DOUBLES = [FakeWorktreeProvider, FakeVmProvider]


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_create_persists_the_providers_locator_and_becomes_ready(config: DatabaseConfig, double: type) -> None:
    async def scenario() -> None:
        adapter = double()
        workspaces = PERSISTENCE.open(config)
        service = WorkspaceService(workspaces, {adapter.provider: adapter})
        try:
            workspace = await service.create(adapter.provider)

            assert workspace.status is WorkspaceStatus.READY
            assert workspace.provider == adapter.provider
            assert workspace.locator
            assert await workspaces.get(workspace.id) == workspace
        finally:
            await workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_ownership_survives_a_restart(config: DatabaseConfig, double: type) -> None:
    """A fresh `WorkspaceService`, as this process gets after it restarts, recovers
    the same workspace through the same provider rather than reprovisioning it."""

    async def scenario() -> None:
        first_adapter = double()
        first_workspaces = PERSISTENCE.open(config)
        try:
            workspace = await first_adapter.create()
            created = await first_workspaces.create(first_adapter.provider, workspace.locator)
        finally:
            await first_workspaces.close()

        restarted_adapter = double()
        restarted_adapter.owned = set(first_adapter.owned)
        restarted_workspaces = PERSISTENCE.open(config)
        restarted_service = WorkspaceService(restarted_workspaces, {restarted_adapter.provider: restarted_adapter})
        try:
            recovered = await restarted_service.recover(created.id)

            assert recovered.id == created.id
            assert recovered.provider == first_adapter.provider
            assert recovered.locator == created.locator
        finally:
            await restarted_workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_destroy_releases_the_workspace_through_its_own_provider(config: DatabaseConfig, double: type) -> None:
    async def scenario() -> None:
        adapter = double()
        workspaces = PERSISTENCE.open(config)
        service = WorkspaceService(workspaces, {adapter.provider: adapter})
        try:
            workspace = await service.create(adapter.provider)

            await service.destroy(workspace.id)

            stopped = await workspaces.get(workspace.id)
            assert stopped is not None and stopped.status is WorkspaceStatus.STOPPED
            assert adapter.destroyed == [workspace.locator]
        finally:
            await workspaces.close()

    asyncio.run(scenario())


# --- Managed lifecycle conformance ------------------------------------------
#
# The same `Lifecycle` scenarios against every `AllocationProvider`: the
# host-shaped double and the real smolvm adapter on its deterministic CLI fake.
# #1054's worktree adapter is expected to add itself to `BACKENDS` and pass
# unchanged. Nothing below names a guest path, image or machine.

from copy import deepcopy  # noqa: E402

from openengine.messaging import Request, envelopes  # noqa: E402

from virtual_control_plane.application.lifecycle import SCHEMAS, Lifecycle  # noqa: E402
from virtual_control_plane.application.ports import Ownership  # noqa: E402
from virtual_control_plane.persistence.durable import DurableStore  # noqa: E402
from virtual_control_plane.providers.smolvm import Smolvm, machine_name  # noqa: E402
from virtual_control_plane.tests import smolvm_fake  # noqa: E402
from virtual_control_plane.tests.fakes import MISSING_TOOL, FakeAllocationProvider  # noqa: E402

APPLY = "oe.v1.vcp.control.workspaces.apply"


class HostBackend:
    """The host-shaped double; compute persists in `allocations` across "restarts"."""

    provider, isolation = "process", "process"

    def __init__(self, tmp_path: Path) -> None:
        self.allocations: dict = {}
        self.current = FakeAllocationProvider(allocations=self.allocations)

    def restart(self) -> FakeAllocationProvider:
        self.current = FakeAllocationProvider(allocations=self.allocations)
        return self.current

    def compute(self) -> list[str]:
        return sorted(self.allocations)

    def make_foreign(self, ownership: Ownership) -> None:
        self.current.foreign.add(ownership.runtime_id)

    def foreign_untouched(self, ownership: Ownership) -> bool:
        return ownership.runtime_id not in self.allocations

    def crash_after_allocation(self) -> None:
        self.current.crash = True


class SmolvmBackend:
    """The real adapter; machines persist in the fake CLI's state directory across restarts."""

    provider, isolation = "smolvm", "microvm"

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "smolvm"
        self.binary = smolvm_fake.install(self.root)
        self.current = self.restart()

    def restart(self) -> Smolvm:
        self.current = Smolvm("example.test/image:1", tools=("git", "terminal", MISSING_TOOL), executable=str(self.binary))
        return self.current

    def compute(self) -> list[str]:
        return sorted(smolvm_fake.machines(self.root))

    def make_foreign(self, ownership: Ownership) -> None:
        self._foreign = {"name": machine_name(ownership), "labels": {"oe.workspace": "ws_someone_else"},
                         "image": "theirs", "state": "created", "cpus": 1, "memory_mib": 256, "storage_gb": None,
                         "network": False, "cmd": []}
        smolvm_fake.save_machine(self.root, self._foreign)

    def foreign_untouched(self, ownership: Ownership) -> bool:
        return smolvm_fake.machines(self.root)[machine_name(ownership)] == self._foreign and all(
            command[1] == "ls" for command in smolvm_fake.commands(self.root))

    def crash_after_allocation(self) -> None:
        # The CLI fake cannot interrupt the adapter mid-call, so interrupt the
        # adapter itself right after its machine exists: the same place a crash
        # between the create and the commit would leave the host.
        adapter, start = self.current, self.current._start

        async def interrupted(name: str) -> None:
            await start(name)
            adapter._start = start
            raise asyncio.CancelledError()

        adapter._start = interrupted


BACKENDS = [HostBackend, SmolvmBackend]


def _document(provider: str, name: str = "example", **spec) -> dict:
    return {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": name},
            "spec": {"provider": provider, "tools": ["git", "terminal"], **spec}}


def _request(subject: str, payload: dict, parameters: dict | None = None, **extra) -> Request:
    return Request(subject, "control", subject.removeprefix("oe.v1.vcp.control."), envelopes.command(payload, **extra),
                   "nats://127.0.0.1:4222", parameters or {})


def _ensure(workspace_id: str) -> Request:
    return _request(f"oe.v1.vcp.control.workspaces.{workspace_id}.ensure", {}, {"workspace_id": workspace_id})


def _store(config: DatabaseConfig) -> DurableStore:
    return DurableStore(PERSISTENCE.database(config))


async def _snapshot(store, collection: str, key: str) -> dict:
    return (await store.read(collection, key))["snapshot"]


async def _ownership(store, workspace_id: str) -> Ownership:
    runtime = await store.read("workspace_runtimes", workspace_id)
    return Ownership(workspace_id, runtime["runtime_id"], runtime["generation"])


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_create_then_idempotent_ensure_and_honest_capabilities(config: DatabaseConfig, tmp_path: Path, backend_type: type) -> None:
    backend = backend_type(tmp_path)

    async def scenario() -> None:
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        try:
            capabilities = await lifecycle.capabilities(Request("oe.v1.vcp.query.capabilities.get", "query",
                                                                "capabilities.get", {"params": {}}, "nats://127.0.0.1:4222"))
            [reported] = capabilities["providers"]
            envelopes.check(f"{SCHEMAS}/capabilities.schema.json#/$defs/provider_capabilities", reported)
            assert reported["isolation"] == backend.isolation and reported["provider"] == backend.provider
            assert reported["tools"] == ["git", "terminal", MISSING_TOOL]
            assert not reported["checkpoint"] and not reported["suspend"] and reported["restore"] == "none"

            created = await lifecycle.apply(_request(APPLY, {"workspace": _document(
                backend.provider, context={"instructions": "Standing orders.", "files": [{"path": "a.txt", "content": "a"}]})}))
            assert created["status"] == "accepted"
            workspace_id = created["resource_id"]
            await lifecycle.reconcile()
            ready = await _snapshot(store, "workspaces", workspace_id)
            envelopes.check(f"{SCHEMAS}/workspace.schema.json#/$defs/workspace", ready)
            assert ready["state"] == "ready" and ready["version"] == 2 and ready["runtime"]["generation"] == 1
            runtime = await store.read("workspace_runtimes", workspace_id)
            assert runtime["provider"] == backend.provider and runtime["locator"]
            assert len(backend.compute()) == 1
            events = [event.envelope["event_type"] for event in await store.pending_events(100)]
            assert events == ["workspace.provisioning", "workspace.ready"]
            context = backend.current.context(await _ownership(store, workspace_id), runtime["locator"],
                                              (await store.read("workspaces", workspace_id))["specification"])
            assert context.root and context.working_directory

            for _ in range(2):
                ensured = await lifecycle.ensure(_ensure(workspace_id))
                assert ensured["status"] == "accepted"
                await lifecycle.reconcile()
                operation = await _snapshot(store, "operations", ensured["operation_id"])
                envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", operation)
                assert operation["status"] == "succeeded" and operation["result"]["restored"] is False
                assert await _snapshot(store, "workspaces", workspace_id) == ready
                assert [event.envelope["event_type"] for event in await store.pending_events(100)] == events
            assert len(backend.compute()) == 1
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_ownership_mismatch_fails_without_touching_the_foreign_compute(config: DatabaseConfig, tmp_path: Path, backend_type: type) -> None:
    backend = backend_type(tmp_path)

    async def scenario() -> None:
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        try:
            created = await lifecycle.apply(_request(APPLY, {"workspace": _document(backend.provider)}))
            workspace_id = created["resource_id"]
            ownership = await _ownership(store, workspace_id)
            backend.make_foreign(ownership)

            await lifecycle.reconcile()

            operation = await _snapshot(store, "operations", created["operation_id"])
            assert operation["status"] == "failed" and operation["error"]["code"] == "PROVIDER_ERROR"
            workspace = await _snapshot(store, "workspaces", workspace_id)
            assert workspace["state"] == "failed" and workspace["failure"] == operation["error"]
            assert backend.foreign_untouched(ownership)
            assert await store.read("workspace_runtimes", workspace_id)  # ownership retained
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_failed_preparation_fails_the_workspace_and_retains_ownership(config: DatabaseConfig, tmp_path: Path, backend_type: type) -> None:
    backend = backend_type(tmp_path)

    async def scenario() -> None:
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        try:
            created = await lifecycle.apply(_request(APPLY, {"workspace": _document(
                backend.provider, tools=["terminal", MISSING_TOOL])}))
            workspace_id = created["resource_id"]

            await lifecycle.reconcile()

            operation = await _snapshot(store, "operations", created["operation_id"])
            assert operation["status"] == "failed" and operation["error"]["retryable"] is False
            workspace = await _snapshot(store, "workspaces", workspace_id)
            assert workspace["state"] == "failed" and "runtime" not in workspace
            assert [event.envelope["event_type"] for event in await store.pending_events(100)] == \
                ["workspace.provisioning", "workspace.failed"]
            assert await store.read("workspace_runtimes", workspace_id)
            rejected = await lifecycle.ensure(_ensure(workspace_id))
            assert rejected["error"]["code"] == "INVALID_STATE"
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_allocation_before_a_crash_is_reconciled_after_restart(config: DatabaseConfig, tmp_path: Path, backend_type: type) -> None:
    backend = backend_type(tmp_path)

    async def scenario() -> None:
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        created = await lifecycle.apply(_request(APPLY, {"workspace": _document(backend.provider)}))
        workspace_id = created["resource_id"]
        backend.crash_after_allocation()
        await lifecycle.reconcile()
        assert (await _snapshot(store, "operations", created["operation_id"]))["status"] == "running"
        assert (await _snapshot(store, "workspaces", workspace_id))["state"] == "provisioning"
        assert len(backend.compute()) == 1
        await store.close()

        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.restart()})
        try:
            await lifecycle.reconcile()

            assert (await _snapshot(store, "operations", created["operation_id"]))["status"] == "succeeded"
            workspace = await _snapshot(store, "workspaces", workspace_id)
            assert workspace["state"] == "ready" and workspace["runtime"]["generation"] == 1
            assert len(backend.compute()) == 1  # the same compute, not a second one
            assert deepcopy(workspace)["runtime"]["runtime_id"] == (await _ownership(store, workspace_id)).runtime_id
        finally:
            await store.close()

    asyncio.run(scenario())
