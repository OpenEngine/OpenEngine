"""Replay Alembic revisions in disposable databases and document their schemas."""

from __future__ import annotations

import argparse
from difflib import unified_diff
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.schema import CreateIndex, CreateTable

ROOT = Path(__file__).resolve().parents[1]


def snapshot(history: Path, revision: str, url: str) -> str:
    """Use a fresh SQLite connection or a rolled-back PostgreSQL transaction."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                config = Config()
                config.set_main_option("script_location", str(history))
                config.attributes["connection"] = connection
                command.upgrade(config, revision)
                if engine.dialect.name == "sqlite":
                    statements = connection.execute(text(
                        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL "
                        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                        "ORDER BY type, name"
                    )).scalars()
                    schema = "\n\n".join(
                        "\n".join(line.rstrip() for line in sql.rstrip(';').splitlines()) + ";"
                        for sql in statements
                    )
                    return schema + ("\n" if schema else "")
                metadata = MetaData()
                metadata.reflect(bind=connection)
                statements = []
                for table in sorted(metadata.tables.values(), key=lambda table: table.name):
                    if table.name == "alembic_version":
                        continue
                    statements.append(str(CreateTable(table).compile(engine)).strip() + ";")
                    statements.extend(
                        str(CreateIndex(index).compile(engine)).strip() + ";"
                        for index in sorted(table.indexes, key=lambda index: index.name)
                    )
                return "\n\n".join(statements) + ("\n" if statements else "")
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def documents(history: Path, url: str) -> dict[str, str]:
    config = Config()
    config.set_main_option("script_location", str(history))
    revisions = list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))
    schemas = {revision.revision: snapshot(history, revision.revision, url) for revision in revisions}
    result = {}
    for revision in revisions:
        schema = schemas[revision.revision]
        parents = revision.down_revision
        parents = (parents,) if isinstance(parents, str) else parents or (None,)
        sections = [f"# {history.name}: {revision.revision}\n", f"{revision.doc}\n"]
        for parent in parents:
            previous = schemas[parent] if parent else ""
            diff = "".join(unified_diff(
                previous.splitlines(keepends=True), schema.splitlines(keepends=True),
                fromfile=parent or "base", tofile=revision.revision,
            ))
            sections.append(f"## Changes from {parent or 'base'}\n\n"
                            + (f"```diff\n{diff}```\n" if diff else "No schema changes.\n"))
        sections.append(f"## Schema\n\n```sql\n{schema}```\n")
        # Markdown diff context includes a space even on blank lines. Normalize
        # display whitespace so generated documents pass repository diff checks.
        result[f"{history.name}/{revision.revision}.md"] = "\n".join(
            line.expandtabs(4).rstrip() for line in "\n".join(sections).splitlines()
        ) + "\n"
    return result


def write_documents(output: Path, generated: dict[str, str]) -> list[str]:
    changed = []
    for name, content in sorted(generated.items()):
        path = output / name
        if not path.exists() or path.read_text() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            changed.append(name)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/schema")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--postgres-url", help="Empty disposable PostgreSQL database URL")
    args = parser.parse_args()
    generated = {}
    for history in ("sqlite", "sqlite_graph"):
        generated.update(documents(ROOT / "migrations" / history, "sqlite://"))
    if args.postgres_url:
        generated.update(documents(ROOT / "migrations/postgres", args.postgres_url))
    changed = write_documents(args.output, generated)
    if args.manifest:
        args.manifest.write_text(json.dumps(changed))
    print(f"Updated {len(changed)} schema documents.")


if __name__ == "__main__":
    main()
