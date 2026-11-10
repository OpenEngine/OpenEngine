from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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


def test_concurrent_applies_neither_duplicate_nor_skip_revisions(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        graphs = PERSISTENCE.open(config)
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
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES))
