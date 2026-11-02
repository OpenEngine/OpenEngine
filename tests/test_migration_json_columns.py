"""Lint: migrations must not add JSON-blob columns without an explicit override.

A JSON-typed column (``sa.JSON``/``JSONB``, however it is wrapped) or a
``*_json`` column hides structure from the schema. Model the fields as columns
or a child table. If a blob is genuinely right, put
``# allow-json-column: <reason>`` (a reason is required) on a line of the
declaration, or on a comment-only line directly above it.

Checked: ``Column(...)`` (any positional/keyword form), ``op.alter_column``
type changes and renames, and raw-SQL DDL strings.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = "allow-json-column:"
OVERRIDE_RE = re.compile(r"#\s*allow-json-column:\s*\S")
JSON_TYPES = {"JSON", "JSONB"}
DDL_RE = re.compile(r"\b(ALTER\s+TABLE|CREATE\s+TABLE|ADD\s+COLUMN)\b", re.I)
SQL_JSON_RE = re.compile(r"\bJSONB?\b|\b\w+_json\s+[A-Za-z]", re.I)

# Migrations that predate this rule, with their exact violation counts. A new
# violation or a fixed one fails the test; do not add entries, use the override.
GRANDFATHERED = {
    "migrations/sqlite/versions/0001_initial_schema.py": 2,
    "migrations/sqlite/versions/9668c9dcd56a_retire_run_states_and_unused_tables.py": 1,
    "migrations/sqlite_graph/versions/3a749697aced_store_workorder_metadata_on_runs.py": 1,
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _json_name(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value.endswith("_json")
    )


def _mentions_json_type(nodes: list[ast.expr]) -> bool:
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Attribute) and node.attr in JSON_TYPES:
                return True
            if isinstance(node, ast.Name) and node.id in JSON_TYPES:
                return True
    return False


def _is_violation(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(DDL_RE.search(node.value) and SQL_JSON_RE.search(node.value))
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    kwargs = {k.arg: k.value for k in node.keywords if k.arg}
    if name == "Column":
        named = [*node.args[:1], *([kwargs["name"]] if "name" in kwargs else [])]
        values = [*node.args, *kwargs.values()]
        return any(_json_name(n) for n in named) or _mentions_json_type(values)
    if name == "alter_column":
        renamed = kwargs.get("new_column_name")
        typed = [kwargs[k] for k in ("type_", "existing_type") if k in kwargs]
        return (renamed is not None and _json_name(renamed)) or _mentions_json_type(
            typed
        )
    return False


def _overridden(lines: list[str], node: ast.AST) -> bool:
    start, end = node.lineno, node.end_lineno or node.lineno
    if any(OVERRIDE_RE.search(line) for line in lines[start - 1 : end]):
        return True
    above = lines[start - 2].strip() if start >= 2 else ""
    return above.startswith("#") and bool(OVERRIDE_RE.search(above))


def json_column_violations(source: str) -> list[int]:
    """Sorted line numbers of JSON-blob column declarations lacking an override."""
    lines = source.splitlines()
    found = {
        node.lineno
        for node in ast.walk(ast.parse(source))
        if _is_violation(node) and not _overridden(lines, node)
    }
    return sorted(found)


def test_flags_json_typed_column() -> None:
    assert json_column_violations('sa.Column("meta", sa.JSON())') == [1]


def test_flags_json_named_text_column() -> None:
    assert json_column_violations('sa.Column("state_json", sa.Text())') == [1]


def test_allows_ordinary_columns() -> None:
    assert json_column_violations('sa.Column("title", sa.Text())') == []


def test_override_comment_allows_a_column() -> None:
    source = "# allow-json-column: opaque provider payload\nsa.Column('p', sa.JSON())\n"
    assert json_column_violations(source) == []


@pytest.mark.parametrize(
    "source",
    [
        'sa.Column("x", type_=sa.JSON())',
        'sa.Column(name="x_json", type_=sa.Text())',
        'sa.Column("x", sa.Text().with_variant(sa.JSON(), "sqlite"))',
        'op.alter_column("t", "x", type_=sa.JSON())',
        'op.alter_column("t", "x", new_column_name="x_json")',
        'op.execute("ALTER TABLE t ADD COLUMN x JSON")',
        'op.execute("ALTER TABLE t ADD COLUMN x_json TEXT")',
    ],
)
def test_flags_variations(source: str) -> None:
    assert json_column_violations(source) == [1]


def test_ignores_json_functions_in_data_migrations() -> None:
    source = "op.execute(\"UPDATE t SET state_json = json_remove(state_json, '$.a')\")"
    assert json_column_violations(source) == []


def test_override_requires_a_reason() -> None:
    source = "# allow-json-column:\nsa.Column('p', sa.JSON())\n"
    assert json_column_violations(source) == [2]


def test_override_on_previous_code_line_does_not_count() -> None:
    source = "sa.Column('a', sa.Text())  # allow-json-column: ok\nsa.Column('b', sa.JSON())\n"
    assert json_column_violations(source) == [2]


def test_override_inside_multiline_declaration() -> None:
    source = "sa.Column(\n    'p',\n    sa.JSON(),  # allow-json-column: opaque\n)\n"
    assert json_column_violations(source) == []


@pytest.mark.parametrize("path", sorted(ROOT.glob("migrations/*/versions/*.py")))
def test_migration_adds_no_json_blob_columns(path: Path) -> None:
    relative = path.relative_to(ROOT).as_posix()
    found = json_column_violations(path.read_text())
    assert len(found) == GRANDFATHERED.get(relative, 0), (
        f"{relative}: JSON-blob column(s) at lines {found}; model relationally, "
        f"add '# {OVERRIDE} <reason>', or (if grandfathered) update the count"
    )
