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


def test_constraint_order_is_canonical_but_column_order_and_literals_are_preserved():
    import sqlite3
    from scripts.document_schema import canonical_sqlite_ddl

    columns = '''
    "z" TEXT DEFAULT 'comma, (text)',
    "a" TEXT CHECK (a IN ('one', 'two''s'))'''
    constraints = ['UNIQUE (z, a)', 'FOREIGN KEY(a) REFERENCES parent(id)',
                   'CONSTRAINT "named (check)" CHECK (z != \'comment -- /* ), */\')']
    left = 'CREATE TABLE "table (name)" (' + columns + ',\n    ' + ',\n    '.join(constraints) + '\n);'
    right = 'CREATE TABLE "table (name)" (' + columns + ',\n    ' + ',\n    '.join(reversed(constraints)) + '\n);'
    assert canonical_sqlite_ddl(left) == canonical_sqlite_ddl(right)
    canonical = canonical_sqlite_ddl(left)
    assert columns in canonical
    with sqlite3.connect(':memory:') as connection:
        connection.execute(canonical)
        assert [row[1] for row in connection.execute('PRAGMA table_info("table (name)")')] == ['z', 'a']
        assert len(list(connection.execute('PRAGMA foreign_key_list("table (name)")'))) == 1
    for sql in ("CREATE INDEX index_name ON t(a)", "CREATE VIRTUAL TABLE t USING fts5(content)"):
        assert canonical_sqlite_ddl(sql) == sql


def test_schema_documents_are_identical_across_independent_replays():
    import subprocess
    import sys
    from engine.ports.sandbox import command_environment

    script = '''import json
from scripts.document_schema import ROOT, documents
print(json.dumps({name: documents(ROOT / 'migrations' / name, 'sqlite://')
                  for name in ('sqlite', 'sqlite_graph')}, sort_keys=True))
'''
    outputs = [subprocess.run([sys.executable, '-c', script], cwd=ROOT,
        env=command_environment({'PYTHONHASHSEED': seed}), check=True,
        capture_output=True, timeout=120).stdout for seed in ('1', '2', '23')]
    assert outputs[0] == outputs[1] == outputs[2]
