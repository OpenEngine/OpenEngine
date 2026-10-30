from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig

from virtual_control_plane.application.transactions import CommandIdConflict
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.persistence.schema import metadata

SUBJECT = "oe.v1.vcp.control.workspaces.apply"
COMMAND = {
    "command_id": "cmd-1",
    "payload": {"workspace": {"name": "example", "provider": "process"}},
    "expected_version": 1,
    "causal_path": ["cause-1", "cmd-1"],
    "deadline_at": "2030-01-01T00:00:00Z",
}
RESPONSE = {"command_id": "cmd-1", "status": "accepted", "operation_id": "op-1", "resource_id": "ws-1"}
RECORDS = {
    "workspaces": {"id": "ws-1", "version": 1, "state": "provisioning"},
    "operations": {"id": "op-1", "state": "pending"},
    "workspace_runtimes": {"workspace_id": "ws-1", "runtime_id": "rt-1", "generation": 3,
                          "provider": "process", "locator": {"path": "/work/ws-1"}},
}


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failure_rolls_back_every_record(config: DatabaseConfig, failure: type[BaseException]) -> None:
    async def scenario() -> None:
        store = DurableStore(PERSISTENCE.database(config))
        try:
            with pytest.raises(failure):
                async with store.transaction() as tx:
                    for collection, value in RECORDS.items():
                        await tx.put(collection, "record", value)
                    await tx.record_command(SUBJECT, COMMAND, RESPONSE)
                    raise failure()
            async with store.transaction() as tx:
                assert await tx.replay(SUBJECT, COMMAND) is None
                for collection in RECORDS:
                    assert await tx.all(collection) == []
                # Retrying after rollback can accept the same ID.
                assert await tx.record_command(SUBJECT, COMMAND, RESPONSE) == RESPONSE
        finally:
            await store.close()

    asyncio.run(scenario())


def test_restart_preserves_snapshots_response_and_failed_ownership(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        store = DurableStore(PERSISTENCE.database(config))
        async with store.transaction() as tx:
            for collection, value in RECORDS.items():
                await tx.put(collection, "record", value)
            await tx.record_command(SUBJECT, COMMAND, RESPONSE)
        await store.close()
        reopened = DurableStore(PERSISTENCE.database(config))
        try:
            async with reopened.transaction() as tx:
                for collection, value in RECORDS.items():
                    assert await tx.get(collection, "record") == value
                    assert await tx.all(collection) == [value]
                await tx.put("operations", "record", {"id": "op-1", "state": "failed"})
            async with reopened.transaction() as tx:
                # Operation failure must not clear ownership or change workspace version.
                assert await tx.get("operations", "record") == {"id": "op-1", "state": "failed"}
                assert await tx.get("workspaces", "record") == RECORDS["workspaces"]
                assert await tx.get("workspace_runtimes", "record") == RECORDS["workspace_runtimes"]
                retry = {**COMMAND, "deadline_at": "2000-01-01T00:00:00Z"}
                original = await tx.replay(SUBJECT, retry)
                assert original == RESPONSE
                original["operation_id"] = "tampered"
                assert await tx.record_command(SUBJECT, retry, {**RESPONSE, "operation_id": "new"}) == RESPONSE
                # Removing a deadline also has no effect on identity.
                retry.pop("deadline_at")
                assert await tx.replay(SUBJECT, retry) == RESPONSE
        finally:
            await reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["subject", "payload", "expected_version", "causal_path"])
def test_command_identity_conflicts_do_not_overwrite_receipts(config: DatabaseConfig, changed: str) -> None:
    async def scenario() -> None:
        store = DurableStore(PERSISTENCE.database(config))
        try:
            async with store.transaction() as tx:
                await tx.record_command(SUBJECT, COMMAND, RESPONSE)
            retry = deepcopy(COMMAND)
            subject = SUBJECT
            if changed == "subject":
                subject += ".other"
            elif changed == "payload":
                retry[changed]["workspace"]["name"] = "different"
            elif changed == "expected_version":
                retry.pop(changed)
            else:
                retry[changed] = ["different-cause", "cmd-1"]
            with pytest.raises(CommandIdConflict, match="COMMAND_ID_CONFLICT"):
                async with store.transaction() as tx:
                    await tx.record_command(subject, retry, RESPONSE)
            async with store.transaction() as tx:
                assert await tx.replay(SUBJECT, COMMAND) == RESPONSE
                with pytest.raises(KeyError):
                    await tx.put("command_receipts", "cmd-1", {})
        finally:
            await store.close()

    asyncio.run(scenario())


def test_rejection_retention_and_json_object_order(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        store = DurableStore(PERSISTENCE.database(config))
        retryable = {"command_id": "cmd-1", "status": "rejected",
                     "error": {"code": "UNAVAILABLE", "message": "try later", "retryable": True}}
        permanent = {**retryable, "error": {"code": "VERSION_CONFLICT", "message": "stale", "retryable": False}}
        try:
            async with store.transaction() as tx:
                assert await tx.record_command(SUBJECT, COMMAND, retryable) == retryable
            async with store.transaction() as tx:
                assert await tx.replay(SUBJECT, COMMAND) is None
                assert await tx.record_command(SUBJECT, COMMAND, permanent) == permanent
            async with store.transaction() as tx:
                reordered = {**COMMAND, "payload": {"workspace": {"provider": "process", "name": "example"}}}
                assert await tx.replay(SUBJECT, reordered) == permanent
                assert await tx.record_command(SUBJECT, COMMAND, RESPONSE) == permanent
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cold_start", range(10))
def test_concurrent_connections_accept_only_once(config: DatabaseConfig, cold_start: int) -> None:
    async def scenario() -> None:
        stores = [DurableStore(PERSISTENCE.database(config)) for _ in range(8)]

        async def accept(store: DurableStore, index: int) -> dict:
            async with store.transaction() as tx:
                reply = await tx.replay(SUBJECT, COMMAND)
                if reply is not None:
                    return reply
                # Yield after reading to expose deferred-transaction races.
                await asyncio.sleep(0)
                reply = {**RESPONSE, "operation_id": f"op-{index}"}
                await tx.put("operations", reply["operation_id"], {"state": "pending"})
                return await tx.record_command(SUBJECT, COMMAND, reply)

        try:
            replies = await asyncio.gather(*(accept(store, i) for i, store in enumerate(stores)))
            assert all(reply == replies[0] for reply in replies)
            async with stores[0].transaction() as tx:
                assert len(await tx.all("operations")) == 1
                assert await tx.replay(SUBJECT, COMMAND) == replies[0]
        finally:
            for store in stores:
                await store.close()

    asyncio.run(scenario())


def test_store_does_not_create_tables_at_runtime(tmp_path: Path) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    database = PERSISTENCE.database(config)

    async def scenario() -> None:
        store = DurableStore(database)
        try:
            with pytest.raises(OperationalError, match="no such table"):
                async with store.transaction() as tx:
                    await tx.get("workspaces", "missing")
        finally:
            await store.close()

    asyncio.run(scenario())
    engine = database.migration_engine()
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_postgres_is_explicitly_unsupported() -> None:
    with pytest.raises(BackendNotImplemented, match="durable VCP operations"):
        DurableStore(PERSISTENCE.database(DatabaseConfig(backend=Backend.POSTGRES)))


@pytest.mark.parametrize("first_version,retry_version", [(1, 1.0), (1.0, 1)])
def test_equivalent_numeric_command_identity_survives_restart(
    config: DatabaseConfig, first_version: int | float, retry_version: int | float,
) -> None:
    from openengine.contracts.validate import load_contract, validate_message

    subject = "oe.v1.vcp.control.workspaces.ws_a1.destroy"
    command = {
        "command_id": "cmd_numeric", "causal_path": ["cmd_numeric"],
        "expected_version": first_version,
        "payload": {"retention": {"artifacts": "retain", "checkpoints": "delete"}},
    }
    retry = {**command, "expected_version": retry_version}
    contract = load_contract("virtual_control_plane")
    assert validate_message(contract, "DestroyCommand", command, subject=subject) == []
    assert validate_message(contract, "DestroyCommand", retry, subject=subject) == []

    async def scenario() -> None:
        store = DurableStore(PERSISTENCE.database(config))
        async with store.transaction() as tx:
            await tx.record_command(subject, command, RESPONSE)
        await store.close()
        reopened = DurableStore(PERSISTENCE.database(config))
        try:
            async with reopened.transaction() as tx:
                assert await tx.replay(subject, retry) == RESPONSE
                assert await tx.record_command(subject, retry, {"different": "response"}) == RESPONSE
                with pytest.raises(CommandIdConflict):
                    await tx.replay(subject, {**retry, "expected_version": 2})
        finally:
            await reopened.close()

    asyncio.run(scenario())


def test_upgrade_preserves_legacy_rows_without_adopting_them(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    import sqlalchemy as sa

    from openengine.persistence.migrations import downgrade

    from virtual_control_plane.persistence.store import Workspace, WorkspaceStatus

    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config, "d8f92154b5f7")
    database = PERSISTENCE.database(config)
    engine = database.migration_engine()
    now = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    rows = [
        {"id": "legacy-path", "provider": "process", "status": "ready",
         "created_at": now, "updated_at": now, "locator": {"path": "/legacy", "opaque": [1, None, True]}},
        {"id": "legacy-null", "provider": "smolvm", "status": "stopped",
         "created_at": now, "updated_at": now, "locator": None},
    ]
    try:
        legacy = sa.Table("workspaces", sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(legacy.insert(), rows)
    finally:
        engine.dispose()
    PERSISTENCE.upgrade(config)
    engine = database.migration_engine()
    try:
        assert set(inspect(engine).get_table_names()) == {
            "alembic_version", "workspaces", "workspace_runtimes", "operations", "command_receipts",
        }
    finally:
        engine.dispose()

    async def scenario() -> None:
        prototype = PERSISTENCE.open(config)
        durable = DurableStore(PERSISTENCE.database(config))
        try:
            for row in rows:
                assert await prototype.get(row["id"]) == Workspace(**row)
            async with durable.transaction() as tx:
                for row in rows:
                    body = await tx.get("workspaces", row["id"])
                    assert set(body) == {"legacy"}
                    assert body["legacy"]["locator"] == row["locator"]
                for collection in ("operations", "workspace_runtimes"):
                    assert await tx.all(collection) == []
                assert await tx.replay(SUBJECT, COMMAND) is None
                await tx.put("workspaces", "managed", RECORDS["workspaces"])
            # Prototype reads and mutations must never interpret managed records.
            assert await prototype.get("managed") is None
            assert await prototype.set_status("managed", WorkspaceStatus.READY) is None
            assert await prototype.set_locator("managed", {}) is None
            assert {workspace.id for workspace in await prototype.list()} == {row["id"] for row in rows}
            updated = await prototype.set_locator("legacy-path", {"path": "/recovered"})
            assert updated.locator == {"path": "/recovered"}
            assert await prototype.set_status("legacy-null", WorkspaceStatus.READY) is not None
            await prototype.close()
            assert await prototype.get("legacy-path") == updated
            async with durable.transaction() as tx:
                assert await tx.get("workspaces", "managed") == RECORDS["workspaces"]
                assert await tx.all("workspace_runtimes") == []
        finally:
            await prototype.close()
            await durable.close()

    asyncio.run(scenario())
    downgrade(PERSISTENCE.migrations, database, "d8f92154b5f7")
    engine = database.migration_engine()
    try:
        legacy = sa.Table("workspaces", sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            restored = {row["id"]: dict(row) for row in connection.execute(legacy.select()).mappings()}
        assert set(restored) == {row["id"] for row in rows}
        assert restored["legacy-path"]["locator"] == {"path": "/recovered"}
        assert restored["legacy-null"]["status"] == "ready"
        assert restored["legacy-null"]["created_at"].replace(tzinfo=UTC) == now
    finally:
        engine.dispose()
    PERSISTENCE.upgrade(config)
    assert PERSISTENCE.drift(metadata, config) == []
