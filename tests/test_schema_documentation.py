"""Schema documents describe actual revisions, including branched histories."""

from pathlib import Path

from scripts.document_schema import ROOT, documents, write_documents


def test_schema_history_and_regeneration(tmp_path: Path) -> None:
    generated = documents(ROOT / "migrations/sqlite", "sqlite://")
    initial = generated["sqlite/sqlite_0001.md"]
    assert "```diff\n--- base" in initial
    assert "+CREATE TABLE" in initial
    assert "alembic_version" not in initial
    latest = generated["sqlite/9668c9dcd56a.md"]
    diff, schema = latest.split("## Schema")
    assert '-CREATE TABLE "run_states"' in diff
    assert 'CREATE TABLE "run_states"' not in schema
    assert "CREATE TABLE legacy_run_metadata" in schema
    assert len(write_documents(tmp_path, generated)) == len(generated)
    assert write_documents(tmp_path, generated) == []
    target = tmp_path / "sqlite/9668c9dcd56a.md"
    target.write_text("stale")
    assert write_documents(tmp_path, generated) == ["sqlite/9668c9dcd56a.md"]


def test_merge_compares_each_parent_without_sibling_contamination() -> None:
    generated = documents(ROOT / "migrations/sqlite_graph", "sqlite://")
    merge = generated["sqlite_graph/c3836c457cf8.md"]
    assert "## Changes from c589719ca92b" in merge
    assert "## Changes from 3a749697aced" in merge
    left = generated["sqlite_graph/c589719ca92b.md"].split("## Schema")[1]
    right = generated["sqlite_graph/3a749697aced.md"].split("## Schema")[1]
    schema = merge.split("## Schema")[1]
    assert left != right
    assert schema != left
    assert schema != right


def test_reserved_postgres_revision_has_no_schema_changes() -> None:
    # The reserved revision has no operations and is dialect-independent.
    generated = documents(ROOT / "migrations/postgres", "sqlite://")
    assert "No schema changes." in generated["postgres/postgres_0001.md"]
