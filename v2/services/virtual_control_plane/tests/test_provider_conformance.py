"""Shared lifecycle conformance: the same assertions against a worktree-shaped
and a VM-shaped `WorkspaceProviderPort` double.

The managed cases below run the real worktree (#1054) and smolvm (#1114)
adapters too; `WorkspaceService` and `Lifecycle` never branch on which
provider they hold, so neither needs a provider-specific lifecycle test.
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
# host-shaped double, the real smolvm adapter on its deterministic CLI fake and
# the real Git worktree adapter on the host. Nothing below names a guest path,
# image, machine or worktree.

import json  # noqa: E402
import os  # noqa: E402
from copy import deepcopy  # noqa: E402

from openengine.messaging import Request, envelopes  # noqa: E402

from virtual_control_plane.application.lifecycle import SCHEMAS, Lifecycle  # noqa: E402
from virtual_control_plane.application.ports import Ownership  # noqa: E402
from virtual_control_plane.persistence.durable import DurableStore  # noqa: E402
from virtual_control_plane.providers.smolvm import Smolvm, machine_name  # noqa: E402
from virtual_control_plane.providers.worktree import Worktree  # noqa: E402
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


class WorktreeBackend:
    """The real worktree adapter; runtime directories persist under its root across restarts.

    No workspace daemon is configured: supervision has its own tests in
    `test_worktree_provider.py` and the public NATS test.
    """

    provider, isolation = "process", "process"

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve() / "worktrees"
        self.current = self.restart()

    def restart(self) -> Worktree:
        self.current = Worktree(self.root, tools=("git", "terminal", MISSING_TOOL))
        return self.current

    def compute(self) -> list[str]:
        return sorted(name for name in os.listdir(self.root) if not name.startswith("."))

    def make_foreign(self, ownership: Ownership) -> None:
        directory = self.root / ownership.runtime_id
        directory.mkdir()
        self._foreign = json.dumps({"provider": "process", "workspace_id": "ws_someone_else",
                                    "runtime_id": ownership.runtime_id, "generation": 1, "repositories": []})
        (directory / "owner.json").write_text(self._foreign)

    def foreign_untouched(self, ownership: Ownership) -> bool:
        directory = self.root / ownership.runtime_id
        return os.listdir(directory) == ["owner.json"] and (directory / "owner.json").read_text() == self._foreign

    def crash_after_allocation(self) -> None:
        # Interrupt right after the owned directory and its files exist, before the commit.
        adapter, prepare = self.current, self.current._prepare

        async def interrupted(*args) -> None:
            await prepare(*args)
            adapter._prepare = prepare
            raise asyncio.CancelledError()

        adapter._prepare = interrupted


BACKENDS = [HostBackend, SmolvmBackend, WorktreeBackend]


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
            # Failed preparation saved ownership but no locator; cleanup still finds its compute.
            assert (await store.read("workspace_runtimes", workspace_id))["locator"] is None
            cleanup = await lifecycle.destroy(_destroy(workspace_id))
            await lifecycle.reconcile()
            assert (await _snapshot(store, "operations", cleanup["operation_id"]))["status"] == "succeeded"
            assert not backend.compute() and await store.read("workspace_runtimes", workspace_id) is None
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


def _destroy(workspace_id: str, **extra) -> Request:
    return _request(f"oe.v1.vcp.control.workspaces.{workspace_id}.destroy",
                    {"retention": {"artifacts": "retain", "checkpoints": "delete"}},
                    {"workspace_id": workspace_id}, **extra)


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
@pytest.mark.parametrize("interruption", ["pending", "running", "deleted"])
def test_destroy_is_restart_safe_and_replays_one_outcome(config, tmp_path, backend_type, interruption):
    backend = backend_type(tmp_path)

    async def scenario():
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        created = await lifecycle.apply(_request(APPLY, {"workspace": _document(backend.provider)}))
        workspace_id = created["resource_id"]
        await lifecycle.reconcile()
        request = _destroy(workspace_id, expected_version=2)
        accepted = await lifecycle.destroy(request)
        assert accepted == await lifecycle.destroy(request)
        assert len(backend.compute()) == 1  # acceptance committed before any external deletion
        assert (await _snapshot(store, "workspaces", workspace_id))["state"] == "destroying"
        assert (await lifecycle.ensure(_ensure(workspace_id)))["error"]["code"] == "UNAVAILABLE"
        if interruption == "running":
            async with store.transaction() as tx:
                record = await tx.get("operations", accepted["operation_id"])
                record["snapshot"]["status"] = "running"
                await tx.put("operations", accepted["operation_id"], record)
        if interruption == "deleted":
            async def crash_before_commit(*args):
                raise asyncio.CancelledError()
            lifecycle._complete = crash_before_commit
            await lifecycle.reconcile()  # actual worker deletes, then crashes before completion
            assert not backend.compute()
            assert (await _snapshot(store, "operations", accepted["operation_id"]))["status"] == "running"
            assert await store.read("workspace_runtimes", workspace_id)
        await lifecycle.close()
        await store.close()

        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.restart()})
        try:
            await lifecycle.reconcile()
            await lifecycle.reconcile()
            assert accepted == await lifecycle.destroy(request)  # stale version still replays
            workspace = await _snapshot(store, "workspaces", workspace_id)
            operation = await _snapshot(store, "operations", accepted["operation_id"])
            assert workspace["state"] == "destroyed" and workspace["version"] == 4 and "runtime" not in workspace
            assert operation["status"] == "succeeded" and operation["result"] == {"retention": request.body["payload"]["retention"]}
            for kind, value in (("workspace", workspace), ("operations", operation)):
                envelopes.check(f"{SCHEMAS}/{kind}.schema.json#/$defs/{'operation' if kind == 'operations' else kind}", value)
            assert not backend.compute() and await store.read("workspace_runtimes", workspace_id) is None
            events = [event.envelope for event in await store.pending_events(100)]
            assert [e["event_type"] for e in events] == ["workspace.provisioning", "workspace.ready", "workspace.destroyed"]
            assert events[-1]["resource_version"] == 4
            assert events[-1]["payload"]["operation"]["operation_id"] == accepted["operation_id"]
            # New commands on tombstones do not touch compute or publish duplicate outcomes.
            repeated = await lifecycle.destroy(_destroy(workspace_id))
            assert (await _snapshot(store, "operations", repeated["operation_id"]))["status"] == "succeeded"
            assert await _snapshot(store, "workspaces", workspace_id) == workspace
            assert len(await store.pending_events(100)) == 3
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_destroy_refuses_foreign_compute_and_retains_ownership(config, tmp_path, backend_type):
    backend = backend_type(tmp_path)

    async def scenario():
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        try:
            created = await lifecycle.apply(_request(APPLY, {"workspace": _document(backend.provider)}))
            workspace_id = created["resource_id"]
            ownership = await _ownership(store, workspace_id)
            backend.make_foreign(ownership)
            await lifecycle.reconcile()  # failed creation, possibly without any persisted locator
            original = await store.read("workspace_runtimes", workspace_id)
            accepted = await lifecycle.destroy(_destroy(workspace_id))
            await lifecycle.reconcile()
            assert (await _snapshot(store, "operations", accepted["operation_id"]))["status"] == "failed"
            assert (await _snapshot(store, "workspaces", workspace_id))["state"] == "failed"
            assert await store.read("workspace_runtimes", workspace_id) == original
            assert backend.foreign_untouched(ownership)
            assert "workspace.destroyed" not in [e.envelope["event_type"] for e in await store.pending_events(100)]
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend_type", BACKENDS, ids=lambda backend: backend.__name__)
def test_destroy_refuses_active_attempts_without_touching_compute(config, tmp_path, backend_type):
    backend = backend_type(tmp_path)

    async def scenario():
        store = _store(config)
        lifecycle = Lifecycle(store, {backend.provider: backend.current})
        try:
            created = await lifecycle.apply(_request(APPLY, {"workspace": _document(backend.provider)}))
            workspace_id = created["resource_id"]
            await lifecycle.reconcile()
            async with store.transaction() as tx:
                workspace = await tx.get("workspaces", workspace_id)
                workspace["snapshot"]["active_attempt_ids"] = ["attempt_live"]
                await tx.put("workspaces", workspace_id, workspace)
            refused = await lifecycle.destroy(_destroy(workspace_id))
            await lifecycle.reconcile()
            assert refused["error"]["code"] == "INVALID_STATE"
            assert len(backend.compute()) == 1 and await store.read("workspace_runtimes", workspace_id)
            assert (await _snapshot(store, "workspaces", workspace_id))["state"] == "ready"
        finally:
            await store.close()

    asyncio.run(scenario())
