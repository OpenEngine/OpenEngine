from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from openengine.persistence import LOCAL_TENANT, Backend, BackendNotImplemented, DatabaseConfig, new_tenant
from openengine.persistence.migrations import downgrade

from graph_engine.persistence import PERSISTENCE
from graph_engine.persistence.schema import metadata
from graph_engine.persistence.store import (
    Command,
    CommandIdConflict,
    DeadlineExceeded,
    Definition,
    VersionConflict,
)


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def test_migrations_match_the_schema(config: DatabaseConfig) -> None:
    assert PERSISTENCE.drift(metadata, config) == []


def test_migrations_roll_back(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    downgrade(PERSISTENCE.migrations, database, "base")

    assert inspect(database.migration_engine()).get_table_names() == ["alembic_version"]


def _command(command_id: str, fingerprint: str = "f1", project: str = "default") -> Command:
    return Command(project, command_id, "oe.v1.ge.control.graphs.apply", fingerprint)


def _definition(source: str, name: str = "triage") -> Definition:
    return Definition(name, None, "resource", source)


def test_an_unchanged_apply_keeps_the_revision_and_a_changed_one_adds_the_next(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            first = await graphs.apply(_command("cmd_1"), _definition('{"a":1}'))
            again = await graphs.apply(_command("cmd_2"), _definition('{"a":1}'))
            changed = await graphs.apply(_command("cmd_3"), _definition('{"a":2}'), expected_version=1)

            graph_id = first["resource_id"]
            assert again["resource_id"] == changed["resource_id"] == graph_id
            results = [(await graphs.operation(reply["operation_id"])).result for reply in (first, again, changed)]
            assert results == [
                {"graph": {"graph_id": graph_id, "revision": 1}, "created": True},
                {"graph": {"graph_id": graph_id, "revision": 1}, "created": False},
                {"graph": {"graph_id": graph_id, "revision": 2}, "created": True},
            ]
            assert (await graphs.graph(graph_id)).latest_revision == 2
            assert (await graphs.revision(graph_id, 1)).source == '{"a":1}'
            assert (await graphs.revision(graph_id, 2)).source_sha256 == _definition('{"a":2}').source_sha256
            assert await graphs.revision(graph_id, 3) is None
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_a_command_id_returns_its_receipt_or_conflicts(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            first = await graphs.apply(_command("cmd_1"), _definition('{"a":1}'))
            assert await graphs.apply(_command("cmd_1"), _definition('{"a":1}')) == first
            with pytest.raises(CommandIdConflict) as conflict:
                await graphs.apply(_command("cmd_1", "f2"), _definition('{"a":2}'))
            assert conflict.value.operation_id == first["operation_id"]

            rejection = {"command_id": "cmd_2", "status": "rejected", "error": {"code": "X", "message": "x", "retryable": False}}
            assert await graphs.reject(_command("cmd_2"), rejection) == rejection
            # A rejected command stays rejected, even once it could be accepted.
            assert await graphs.apply(_command("cmd_2"), _definition('{"a":3}')) == rejection
            assert (await graphs.graph(first["resource_id"])).latest_revision == 1
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_expected_version_must_be_the_latest_revision(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            with pytest.raises(VersionConflict) as missing:
                await graphs.apply(_command("cmd_1"), _definition('{"a":1}'), expected_version=1)
            assert missing.value.actual is None
            await graphs.apply(_command("cmd_2"), _definition('{"a":1}'))
            with pytest.raises(VersionConflict) as stale:
                await graphs.apply(_command("cmd_3"), _definition('{"a":2}'), expected_version=2)
            assert (stale.value.expected, stale.value.actual) == (2, 1)
            # A conflict records nothing, so the command ID is still free.
            assert (await graphs.apply(_command("cmd_1"), _definition('{"a":1}')))["status"] == "accepted"
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_the_deadline_is_checked_once_the_apply_holds_the_lock(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            accepted = await graphs.apply(_command("cmd_1"), _definition("{}"))
            # Waiting behind another write carries this apply past its deadline.
            async with graphs._writing:  # noqa: SLF001
                waiting = asyncio.create_task(graphs.apply(
                    _command("cmd_2"), _definition("[]"), deadline=datetime.now(UTC) + timedelta(milliseconds=50)
                ))
                await asyncio.sleep(0.1)
            with pytest.raises(DeadlineExceeded):
                await waiting
            assert (await graphs.graph(accepted["resource_id"])).latest_revision == 1
            # An accepted command's receipt is replayed whatever its deadline.
            past = datetime.now(UTC) - timedelta(days=1)
            assert await graphs.apply(_command("cmd_1"), _definition("{}"), deadline=past) == accepted
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_request_scoped_writes_share_the_database_queue(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        persistence = PERSISTENCE.open(config)
        first = persistence.scoped(LOCAL_TENANT)
        second = persistence.scoped(LOCAL_TENANT)
        other = persistence.scoped(new_tenant())
        tasks = []
        try:
            async with first._writing:  # noqa: SLF001
                tasks = [
                    asyncio.create_task(second.apply(_command("queued"), _definition("{}"))),
                    asyncio.create_task(other.reject(_command("queued"), {"status": "rejected"})),
                ]
                await asyncio.sleep(0.05)
                assert all(not task.done() for task in tasks)
                assert await second.graphs("default", 10) == []
            accepted, rejected = await asyncio.gather(*tasks)
            assert accepted["status"] == "accepted"
            assert rejected == {"status": "rejected"}
            assert await second.apply(_command("queued"), _definition("{}")) == accepted
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            await persistence.close()

    asyncio.run(scenario())


def test_concurrent_applies_neither_duplicate_nor_skip_revisions(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            same = await asyncio.gather(*(graphs.apply(_command(f"cmd_same_{n}"), _definition("{}")) for n in range(8)))
            changed = await asyncio.gather(
                *(graphs.apply(_command(f"cmd_changed_{n}"), _definition(f'{{"n":{n}}}')) for n in range(8))
            )
            retried = await asyncio.gather(*(graphs.apply(_command("cmd_retried"), _definition("[]")) for _ in range(8)))

            created = [(await graphs.operation(reply["operation_id"])).result["created"] for reply in same]
            assert sorted(created) == [False] * 7 + [True]
            revisions = [(await graphs.operation(reply["operation_id"])).result["graph"]["revision"] for reply in changed]
            assert sorted(revisions) == list(range(2, 10))
            assert len({reply["operation_id"] for reply in retried}) == 1
            assert (await graphs.graph(same[0]["resource_id"])).latest_revision == 10
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_graphs_are_listed_by_name_and_paged_after_a_key(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            for name in ("review", "triage", "review-fast", "Review", "re_view", "deploy"):
                await graphs.apply(_command(f"cmd_{name}"), _definition("{}", name))

            assert [graph.name for graph in await graphs.graphs("default", 50)] == [
                "Review", "deploy", "re_view", "review", "review-fast", "triage",
            ]
            # A prefix matches exactly: case counts, and `_` is no wildcard.
            assert [graph.name for graph in await graphs.graphs("default", 50, "review")] == ["review", "review-fast"]
            assert [graph.name for graph in await graphs.graphs("default", 50, "re_")] == ["re_view"]
            assert await graphs.graphs("default", 50, "x") == []

            first = await graphs.graphs("default", 2)
            assert [graph.name for graph in first] == ["Review", "deploy"]
            rest = await graphs.graphs("default", 50, after=(first[-1].name, first[-1].graph_id))
            assert [graph.name for graph in rest] == ["re_view", "review", "review-fast", "triage"]
            assert [graph.name for graph in await graphs.graphs("default", 50, "review", ("review", rest[1].graph_id))] == ["review-fast"]
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_names_command_ids_and_listings_are_per_project(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            alpha = await graphs.apply(_command("cmd_1", project="alpha"), _definition('{"a":1}'))
            # The same name and command ID in another project are another graph and another command.
            beta = await graphs.apply(_command("cmd_1", "f2", project="beta"), _definition('{"b":1}'))
            assert alpha["resource_id"] != beta["resource_id"]
            assert await graphs.apply(_command("cmd_1", project="alpha"), _definition('{"a":1}')) == alpha
            with pytest.raises(CommandIdConflict):
                await graphs.apply(_command("cmd_1", "f3", project="beta"), _definition('{"b":2}'))

            again = await graphs.apply(_command("cmd_2", project="alpha"), _definition('{"a":1}'))
            assert (await graphs.operation(again["operation_id"])).result["created"] is False
            # expected_version names the latest revision of the graph in the command's project.
            with pytest.raises(VersionConflict) as missing:
                await graphs.apply(_command("cmd_3", project="gamma"), _definition('{"a":1}'), expected_version=1)
            assert missing.value.actual is None

            assert [(graph.project, graph.graph_id) for graph in await graphs.graphs("alpha", 50)] == [
                ("alpha", alpha["resource_id"]),
            ]
            assert [graph.graph_id for graph in await graphs.graphs("beta", 50)] == [beta["resource_id"]]
            assert await graphs.graphs("default", 50) == []
            assert (await graphs.graph(beta["resource_id"])).project == "beta"
            assert (await graphs.operation(beta["operation_id"])).project == "beta"
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_the_project_migration_keeps_existing_graphs_under_default(tmp_path: Path) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config, "4925ef9f3b79")
    now = "2026-10-27 00:00:00.000000"  # as SQLite keeps a timestamp
    with PERSISTENCE.database(config).migration_engine().begin() as connection:
        for graph_id, name, latest in (("graph_a", "triage", 2), ("graph_b", "review", 1)):
            connection.execute(sa.text(
                "INSERT INTO graphs VALUES (:graph_id, :name, NULL, :latest, :now, :now)"
            ), {"graph_id": graph_id, "name": name, "latest": latest, "now": now})
            for revision in range(1, latest + 1):
                connection.execute(sa.text(
                    "INSERT INTO graph_revisions VALUES (:graph_id, :revision, 'resource', :source, :sha, :now)"
                ), {"graph_id": graph_id, "revision": revision, "source": f'{{"r":{revision}}}',
                    "sha": _definition(f'{{"r":{revision}}}').source_sha256, "now": now})
        connection.execute(sa.text(
            "INSERT INTO operations VALUES ('op_1', 'cmd_old', 'graphs.apply', 'succeeded', '{}', NULL, NULL, :now, :now)"
        ), {"now": now})
        connection.execute(sa.text(
            "INSERT INTO command_receipts VALUES ('cmd_old', 'oe.v1.ge.control.graphs.apply', 'f1', :response, :now)"
        ), {"response": '{"command_id": "cmd_old", "status": "accepted", "operation_id": "op_1"}', "now": now})

    PERSISTENCE.upgrade(config)

    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            listed = await graphs.graphs("default", 50)
            assert [(graph.graph_id, graph.project, graph.name, graph.latest_revision) for graph in listed] == [
                ("graph_b", "default", "review", 1), ("graph_a", "default", "triage", 2),
            ]
            assert (await graphs.revision("graph_a", 2)).source == '{"r":2}'
            assert (await graphs.operation("op_1")).project == "default"
            # The receipt is still the default project's, and an unchanged apply there is still a no-op.
            assert (await graphs.apply(_command("cmd_old"), _definition("{}")))["operation_id"] == "op_1"
            unchanged = await graphs.apply(_command("cmd_new"), _definition('{"r":2}', "triage"))
            assert unchanged["resource_id"] == "graph_a"
            assert (await graphs.operation(unchanged["operation_id"])).result["created"] is False
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_any_revision_of_a_graph_is_read_back(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            graph_id = (await graphs.apply(_command("cmd_1"), _definition('{"a":1}')))["resource_id"]
            await graphs.apply(_command("cmd_2"), _definition('{"a":2}'))

            latest = await graphs.graph(graph_id)
            assert (latest.name, latest.latest_revision) == ("triage", 2)
            first, second = await graphs.revision(graph_id, 1), await graphs.revision(graph_id, latest.latest_revision)
            assert (first.revision, first.format, first.source) == (1, "resource", '{"a":1}')
            assert (second.revision, second.source) == (2, '{"a":2}')
            assert first.source_sha256 != second.source_sha256
            assert await graphs.graph("graph_missing") is None
            assert await graphs.revision("graph_missing", 1) is None
            assert await graphs.revision(graph_id, 3) is None
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_postgres_store_is_a_stub() -> None:
    with pytest.raises(BackendNotImplemented, match="graph engine's store"):
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES)).scoped(LOCAL_TENANT)


def test_two_tenants_isolate_graphs_revisions_operations_pages_and_receipts(config: DatabaseConfig) -> None:
    from openengine.persistence import new_tenant, assert_tenant_scoped

    assert assert_tenant_scoped(metadata, "ge") == []

    async def scenario() -> None:
        handle = PERSISTENCE.open(config)
        alpha, beta = handle.scoped(new_tenant()), handle.scoped(new_tenant())
        try:
            a, b = await asyncio.gather(
                alpha.apply(_command("shared"), _definition("alpha")),
                beta.apply(_command("shared", "beta"), _definition("beta")),
            )
            assert a != b
            assert await alpha.apply(_command("shared"), _definition("alpha")) == a
            assert await beta.apply(_command("shared", "beta"), _definition("beta")) == b
            for own, other, reply in ((alpha, beta, a), (beta, alpha, b)):
                assert await other.graph(reply["resource_id"]) is None
                assert await other.revision(reply["resource_id"], 1) is None
                assert await other.operation(reply["operation_id"]) is None
                assert (await own.graph(reply["resource_id"])).name == "triage"
                assert len(await own.graphs("default", 1)) == 1
                assert await own.graphs("default", 1, after=("triage", reply["resource_id"])) == []
                with pytest.raises(CommandIdConflict) as conflict:
                    await own.apply(_command("shared", "different"), _definition("x"))
                assert conflict.value.operation_id == reply["operation_id"]
            rejection = {"status": "rejected", "error": {"message": "alpha only"}}
            await alpha.reject(_command("rejected"), rejection)
            accepted = await beta.apply(_command("rejected"), _definition("beta"))
            assert accepted["status"] == "accepted"
            assert await alpha.apply(_command("rejected"), _definition("alpha")) == rejection
        finally:
            await handle.close()

    asyncio.run(scenario())


def test_tenant_migration_refuses_unsafe_downgrade_and_cross_tenant_foreign_keys(config: DatabaseConfig) -> None:
    from openengine.persistence import new_tenant
    tenant = new_tenant()

    async def populate() -> str:
        handle = PERSISTENCE.open(config)
        try:
            return (await handle.scoped(tenant).apply(_command("cmd"), _definition("{}")))["resource_id"]
        finally:
            await handle.close()

    graph_id = asyncio.run(populate())
    engine = PERSISTENCE.database(config).migration_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(
                "INSERT INTO graph_revisions (tenant_id, graph_id, revision, format, source, source_sha256, created_at) "
                "VALUES (:tenant, :graph, 2, 'resource', '{}', 'sha', '2026-10-27')"
            ), {"tenant": LOCAL_TENANT.hex, "graph": graph_id})
    with pytest.raises(RuntimeError, match="non-local tenant rows"):
        downgrade(PERSISTENCE.migrations, PERSISTENCE.database(config), "d75ffe6cc5ed")
    assert PERSISTENCE.drift(metadata, config) == []


def test_populated_local_tenant_rows_survive_downgrade_and_upgrade(config: DatabaseConfig) -> None:
    async def populate() -> dict:
        handle = PERSISTENCE.open(config)
        try:
            store = handle.scoped(LOCAL_TENANT)
            reply = await store.apply(_command("accepted"), _definition("first"))
            await store.apply(_command("changed"), _definition("second"))
            await store.reject(_command("rejected"), {"status": "rejected", "error": {"message": "legacy"}})
            return reply
        finally:
            await handle.close()

    reply = asyncio.run(populate())
    downgrade(PERSISTENCE.migrations, PERSISTENCE.database(config), "d75ffe6cc5ed")
    PERSISTENCE.upgrade(config)
    assert PERSISTENCE.drift(metadata, config) == []

    async def replay() -> None:
        handle = PERSISTENCE.open(config)
        try:
            store = handle.scoped(LOCAL_TENANT)
            assert await store.apply(_command("accepted"), _definition("first")) == reply
            assert (await store.apply(_command("rejected"), _definition("{}")))["error"]["message"] == "legacy"
            assert (await store.revision(reply["resource_id"], 1)).source == "first"
            assert (await store.revision(reply["resource_id"], 2)).source == "second"
        finally:
            await handle.close()

    asyncio.run(replay())
