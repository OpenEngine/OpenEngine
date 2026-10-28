from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openengine.persistence import Backend, ServicePersistence
from openengine.persistence.documentation import comments, diagram, discover, generate, main, write
from openengine.persistence.migrations import revision, scaffold

SERVICES = Path(__file__).resolve().parents[3] / "services"

CREATE = """
    op.create_table("jobs", sa.Column("id", sa.Integer, primary_key=True), sa.Column("title", sa.String))
    op.create_table("runs", sa.Column("id", sa.Integer, primary_key=True))"""
CHANGE = """
    op.drop_table("runs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("title")
        batch.add_column(sa.Column("name", sa.String, nullable=False))
    op.create_table("loops", sa.Column("id", sa.Integer, primary_key=True))"""


def history(directory: Path) -> tuple[ServicePersistence[Any], list[str]]:
    persistence = ServicePersistence[Any]("jobs", directory, {Backend.SQLITE: object})
    scaffold(persistence.migrations)
    revisions = []
    for message, body in (("create jobs", CREATE), ("rework jobs", CHANGE)):
        script = revision(persistence.migrations, message)
        path = Path(script.path)
        path.write_text(path.read_text().replace("def upgrade() -> None:\n    pass", f"def upgrade() -> None:{body}"))
        revisions.append(script.revision)
    return persistence, revisions


def test_each_revisions_erd_highlights_what_it_changed(tmp_path: Path) -> None:
    persistence, (first, second) = history(tmp_path)

    files = generate(persistence)

    created = files[f"jobs/{first}.mermaid"]
    assert "class jobs added" in created and "class runs added" in created
    changed = files[f"jobs/{second}.mermaid"]
    assert "class jobs changed" in changed
    assert "class runs removed" in changed
    assert "class loops added" in changed
    assert 'title "REMOVED;' in changed
    assert 'name "ADDED; not null"' in changed
    assert files["latest_jobs.mermaid"] == changed
    document = files[f"jobs/{second}.md"]
    assert f"![jobs at {second}]({second}.svg)" in document
    assert "-CREATE TABLE runs (" in document


def test_an_unchanged_schema_has_no_highlights() -> None:
    tables = {"jobs": {"columns": {"id": {"type": "INTEGER", "nullable": False, "default": None, "keys": ["PK"]}},
                       "foreign_keys": [], "unique": [], "indexes": [], "checks": []}}

    output = diagram("jobs", tables, tables)

    assert "    class " not in output
    assert 'INTEGER id PK "not null"' in output


def test_rewrites_only_what_changed_or_was_never_rendered(tmp_path: Path) -> None:
    files = {"jobs/a.mermaid": "erDiagram\n", "jobs/a.md": "# a\n"}

    assert write(tmp_path, files) == ["jobs/a.md", "jobs/a.mermaid"]
    assert write(tmp_path, files) == ["jobs/a.mermaid"]  # still has no SVG
    (tmp_path / "jobs/a.svg").write_text("<svg/>")
    assert write(tmp_path, files) == []


def test_manifest_lists_changed_paths_and_the_svgs_to_render(tmp_path: Path) -> None:
    output, manifest = tmp_path / "docs", tmp_path / "changes.json"

    main(["--services", str(SERVICES), "--output", str(output), "--manifest", str(manifest)])

    changed = json.loads(manifest.read_text())
    assert str(output / "latest_cxn.mermaid") in changed
    assert str(output / "latest_cxn.svg") in changed
    assert {path.split("/")[-2] for path in changed if path.endswith(".md")} == {"cxn", "ge", "tools", "vcp"}


def test_every_service_table_has_a_comment() -> None:
    for persistence in discover(SERVICES):
        for table, comment in comments(persistence).items():
            assert comment, f"{persistence.name}.{table} has no comment"


def test_a_tables_comment_is_drawn_into_its_entity() -> None:
    tables = {"jobs": {"columns": {"id": {"type": "INTEGER", "nullable": False, "default": None, "keys": ["PK"]}},
                       "foreign_keys": [], "unique": [], "indexes": [], "checks": []}}

    output = diagram("jobs", tables, {}, {"jobs": "Queued units of work."})

    assert '    jobs["jobs<br/>Queued units of work. (ADDED)"] {' in output


def test_a_quote_in_a_tables_comment_does_not_corrupt_its_label() -> None:
    tables = {"jobs": {"columns": {"id": {"type": "INTEGER", "nullable": False, "default": None, "keys": ["PK"]}},
                       "foreign_keys": [], "unique": [], "indexes": [], "checks": []}}

    output = diagram("jobs", tables, {}, {"jobs": 'The "queue" of work.'})

    assert '    jobs["jobs<br/>The \'queue\' of work. (ADDED)"] {' in output
