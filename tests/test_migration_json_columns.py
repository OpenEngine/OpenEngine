"""Lint: migrations must not add JSON-blob columns without an explicit override.

A JSON-typed column (``sa.JSON``/``JSONB``) or a ``*_json`` text column hides
structure from the schema. Model the fields as columns or a child table. If a
blob is genuinely right, put ``# allow-json-column: <reason>`` on the line that
declares the column (or the line above it).
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = "allow-json-column:"
JSON_TYPES = {"JSON", "JSONB"}

# Migrations that predate this rule. Do not add to this set; use the override.
GRANDFATHERED = {
    "migrations/sqlite/versions/0001_initial_schema.py",
    "migrations/sqlite/versions/9668c9dcd56a_retire_run_states_and_unused_tables.py",
    "migrations/sqlite_graph/versions/3a749697aced_store_workorder_metadata_on_runs.py",
}


def _type_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def json_column_violations(source: str) -> list[int]:
    """Line numbers of ``Column(...)`` declarations that are JSON blobs."""
    lines = source.splitlines()
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and _type_name(node) == "Column"):
            continue
        args = node.args
        named = (
            args
            and isinstance(args[0], ast.Constant)
            and isinstance(args[0].value, str)
            and args[0].value.endswith("_json")
        )
        typed = any(_type_name(a) in JSON_TYPES for a in args)
        if not (named or typed):
            continue
        span = lines[max(node.lineno - 2, 0) : node.end_lineno]
        if not any(OVERRIDE in line for line in span):
            found.append(node.lineno)
    return found


def test_flags_json_typed_column() -> None:
    assert json_column_violations('sa.Column("meta", sa.JSON())') == [1]


def test_flags_json_named_text_column() -> None:
    assert json_column_violations('sa.Column("state_json", sa.Text())') == [1]


def test_allows_ordinary_columns() -> None:
    assert json_column_violations('sa.Column("title", sa.Text())') == []


def test_override_comment_allows_a_column() -> None:
    source = "# allow-json-column: opaque provider payload\nsa.Column('p', sa.JSON())\n"
    assert json_column_violations(source) == []


@pytest.mark.parametrize("path", sorted(ROOT.glob("migrations/*/versions/*.py")))
def test_migration_adds_no_json_blob_columns(path: Path) -> None:
    relative = path.relative_to(ROOT).as_posix()
    if relative in GRANDFATHERED:
        pytest.skip("predates the JSON-column rule")
    assert json_column_violations(path.read_text()) == [], (
        f"{relative}: JSON-blob column; model it relationally or add "
        f"'# {OVERRIDE} <reason>'"
    )
