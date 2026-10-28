"""Document every service's schema at each revision of its migration history.

For each revision this writes `<output>/<service>/<revision>.md`, the schema's
SQL and its diff from the parent revision, and `<revision>.mermaid`, an ERD
highlighting what the revision added, removed or changed, with each table's
comment from its schema module. The head revision's ERD is also written to
`<output>/latest_<service>.mermaid`. Mermaid CLI renders each changed
`.mermaid` to the `.svg` beside it.

Every revision is compared with its parent rather than a branch, so the output
depends only on the migrations and regenerating it is a no-op.

    python -m openengine.persistence.documentation --services services --output docs/schema
"""

from __future__ import annotations

import argparse
import importlib
import json
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text

from openengine.persistence import migrations
from openengine.persistence.service import ServicePersistence
from openengine.persistence.sqlite import SqliteDatabase

Tables = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Snapshot:
    revision: str
    message: str
    parents: tuple[str | None, ...]
    sql: str
    tables: Tables


def _sql(connection: Connection) -> str:
    statements = connection.execute(
        text(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY type, name"
        )
    ).scalars()
    schema = "\n\n".join("\n".join(line.rstrip() for line in sql.rstrip(";").splitlines()) + ";" for sql in statements)
    return schema + ("\n" if schema else "")


def _tables(connection: Connection) -> Tables:
    inspector = inspect(connection)
    tables = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        pk = inspector.get_pk_constraint(table)["constrained_columns"]
        foreign_keys = inspector.get_foreign_keys(table)
        unique = inspector.get_unique_constraints(table)
        columns = {}
        for column in inspector.get_columns(table):
            name = column["name"]
            keys = (
                ("PK", name in pk),
                ("FK", any(name in fk["constrained_columns"] for fk in foreign_keys)),
                ("UK", any(name in constraint["column_names"] for constraint in unique)),
            )
            columns[name] = {
                "type": str(column["type"]),
                "nullable": column["nullable"],
                "default": column["default"],
                "keys": [key for key, present in keys if present],
            }
        tables[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "unique": unique,
            "indexes": inspector.get_indexes(table),
            "checks": inspector.get_check_constraints(table),
        }
    return tables


def snapshots(persistence: ServicePersistence[Any]) -> list[Snapshot]:
    """The schema at every revision, oldest first, each migrated into a fresh SQLite database."""
    config = Config()
    config.set_main_option("script_location", str(persistence.migrations))
    revisions = list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))
    result = []
    for revision in revisions:
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(persistence.name, Path(directory))
            migrations.upgrade(persistence.migrations, database, revision.revision)
            engine = database.migration_engine()
            try:
                with engine.connect() as connection:
                    sql, tables = _sql(connection), _tables(connection)
            finally:
                engine.dispose()
        parents = revision.down_revision
        parents = (parents,) if isinstance(parents, str) else tuple(parents or (None,))
        result.append(Snapshot(revision.revision, revision.doc or "", parents, sql, tables))
    return result


def comments(persistence: ServicePersistence[Any]) -> dict[str, str]:
    """Each of the service's tables and its comment, from its schema module; empty for a table that names none."""
    try:
        schema = importlib.import_module(f"{persistence.directory.parent.name}.persistence.schema")
    except ModuleNotFoundError:
        return {}
    return {table.name: table.comment or "" for table in schema.metadata.tables.values()}


def diagram(title: str, current: Tables, previous: Tables, comments: Mapping[str, str] = {}) -> str:
    """An ERD of `current`, with what changed since `previous` highlighted and removals kept as tombstones."""
    lines = [
        "---",
        f"title: {title} (green added, red removed, amber changed)",
        "config:",
        "  theme: neutral",
        "---",
        "erDiagram",
        "    direction LR",
        "    %% REMOVED tables/columns are tombstones, absent from this revision's schema.",
        "    %% Column changes include type, nullability, default and key membership.",
        "    %% Table changes also include indexes and constraints.",
        "    classDef added fill:#dcfce7,stroke:#15803d,color:#14532d",
        "    classDef removed fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
        "    classDef changed fill:#fef3c7,stroke:#b45309,color:#78350f",
    ]
    for name in sorted(current.keys() | previous.keys()):
        now, old = current.get(name), previous.get(name)
        status = "added" if old is None else "removed" if now is None else "changed" if now != old else ""
        label = f"{name}<br/>{comments[name]}" if comments.get(name) else name
        lines.append(f'    {name}["{label}{f" ({status.upper()})" if status else ""}"] {{')
        table = now or old
        assert table is not None
        columns, old_columns = table["columns"], old["columns"] if old else {}
        for column in dict.fromkeys([*columns, *old_columns]):
            value = columns.get(column, old_columns.get(column))
            change = (
                "REMOVED"
                if now is None or column not in columns
                else "ADDED"
                if column not in old_columns
                else "CHANGED"
                if value != old_columns[column]
                else ""
            )
            keys = " " + ",".join(value["keys"]) if value["keys"] else ""
            note = "nullable" if value["nullable"] else "not null"
            if change:
                note = f"{change}; {note}"
            if value["default"] is not None:
                note += "; default: " + str(value["default"]).replace('"', "'")
            lines.append(f'        {value["type"]} {column}{keys} "{note}"')
        lines.append("    }")
        if status:
            lines.append(f"    class {name} {status}")
        for fk in table["foreign_keys"]:
            nullable = any(columns[column]["nullable"] for column in fk["constrained_columns"])
            label = ", ".join(fk["constrained_columns"]) + (" (REMOVED)" if now is None else "")
            lines.append(f'    {fk["referred_table"]} {"|o" if nullable else "||"}--o{{ {name} : "{label}"')
    return "\n".join(lines) + "\n"


def document(service: str, snapshot: Snapshot, by_revision: dict[str, Snapshot]) -> str:
    sections = [
        f"# {service}: {snapshot.revision}\n",
        f"{snapshot.message}\n",
        f"![{service} at {snapshot.revision}]({snapshot.revision}.svg)\n",
    ]
    for parent in snapshot.parents:
        previous = by_revision[parent].sql if parent else ""
        diff = "".join(
            unified_diff(
                previous.splitlines(keepends=True),
                snapshot.sql.splitlines(keepends=True),
                fromfile=parent or "base",
                tofile=snapshot.revision,
            )
        )
        changes = f"```diff\n{diff}```\n" if diff else "No schema changes.\n"
        sections.append(f"## Changes from {parent or 'base'}\n\n{changes}")
    sections.append(f"## Schema\n\n```sql\n{snapshot.sql}```\n")
    # Diff context puts a space on blank lines; strip it so the documents pass whitespace checks.
    return "\n".join(line.expandtabs(4).rstrip() for line in "\n".join(sections).splitlines()) + "\n"


def generate(persistence: ServicePersistence[Any]) -> dict[str, str]:
    """Every document for one service, keyed by its path relative to the output directory."""
    history = snapshots(persistence)
    by_revision = {snapshot.revision: snapshot for snapshot in history}
    service = persistence.name
    table_comments = comments(persistence)
    files = {}
    for snapshot in history:
        parent = snapshot.parents[0]
        previous = by_revision[parent].tables if parent else {}
        erd = diagram(f"{service} at {snapshot.revision}", snapshot.tables, previous, table_comments)
        files[f"{service}/{snapshot.revision}.mermaid"] = erd
        files[f"{service}/{snapshot.revision}.md"] = document(service, snapshot, by_revision)
    if history:
        files[f"latest_{service}.mermaid"] = files[f"{service}/{history[-1].revision}.mermaid"]
    return files


def write(output: Path, files: dict[str, str]) -> list[str]:
    """Write the files whose content changed, or whose rendered SVG is missing; return their paths."""
    changed = []
    for name, content in sorted(files.items()):
        path = output / name
        svg = path.with_suffix(".svg")
        if path.exists() and path.read_text() == content and (path.suffix != ".mermaid" or svg.exists()):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        changed.append(name)
    return changed


def discover(services: Path) -> Iterable[ServicePersistence[Any]]:
    """Each service under `services` that declares its persistence."""
    for package in sorted(services.glob("*/persistence/__init__.py")):
        yield importlib.import_module(f"{package.parent.parent.name}.persistence").PERSISTENCE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--services", type=Path, default=Path("services"))
    parser.add_argument("--output", type=Path, default=Path("docs/schema"))
    parser.add_argument("--manifest", type=Path, help="write the changed paths, and the SVGs to render for them, here as JSON")
    arguments = parser.parse_args(argv)
    files: dict[str, str] = {}
    for persistence in discover(arguments.services):
        files.update(generate(persistence))
    changed = write(arguments.output, files)
    if arguments.manifest:
        paths = [arguments.output / name for name in changed]
        paths += [path.with_suffix(".svg") for path in paths if path.suffix == ".mermaid"]
        arguments.manifest.write_text(json.dumps(sorted(str(path) for path in paths)))
    print(f"Updated {len(changed)} schema documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
