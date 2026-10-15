"""Render the current SQLite schemas, highlighting changes from a baseline checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def schemas(root: Path) -> dict:
    result = {}
    for history, prefix in (("sqlite", "state"), ("sqlite_graph", "graph")):
        location = root / "migrations" / history
        if not location.exists():
            continue
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as connection:
                config = Config()
                config.set_main_option("script_location", str(location.resolve()))
                config.attributes["connection"] = connection
                command.upgrade(config, "heads")
                inspector = inspect(connection)
                for table in inspector.get_table_names():
                    if table == "alembic_version":
                        continue
                    pk = inspector.get_pk_constraint(table)["constrained_columns"]
                    fks = inspector.get_foreign_keys(table)
                    unique = inspector.get_unique_constraints(table)
                    columns = {}
                    for column in inspector.get_columns(table):
                        name = column["name"]
                        columns[name] = {
                            "type": str(column["type"]), "nullable": column["nullable"],
                            "default": column["default"],
                            "keys": [key for key, present in (
                                ("PK", name in pk),
                                ("FK", any(name in fk["constrained_columns"] for fk in fks)),
                                ("UK", any(name in uk["column_names"] for uk in unique)),
                            ) if present],
                        }
                    result[f"{prefix}_{table}"] = {
                        "columns": columns, "pk": pk, "foreign_keys": fks,
                        "unique": unique, "indexes": inspector.get_indexes(table),
                        "checks": inspector.get_check_constraints(table),
                    }
        finally:
            engine.dispose()
    return result


def diagram(current: dict, previous: dict) -> str:
    lines = [
        '---', 'title: Latest schema (green added, red removed, amber changed)',
        'config:', '  theme: neutral', '---',
        'erDiagram', '    direction LR',
        '    %% state_ and graph_ are separate SQLite databases, not SQL name prefixes.',
        '    %% REMOVED tables/columns are comparison-only tombstones, absent from the latest schema.',
        '    %% Column changes include type, nullability, default and key membership.',
        '    %% Table changes also include indexes and constraints.',
        '    %% Solid edges are declared FKs; dashed edges are selected logical references.',
        '    classDef added fill:#dcfce7,stroke:#15803d,color:#14532d',
        '    classDef removed fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d',
        '    classDef changed fill:#fef3c7,stroke:#b45309,color:#78350f',
    ]
    for name in sorted(current.keys() | previous.keys()):
        now, old = current.get(name), previous.get(name)
        status = 'added' if old is None else 'removed' if now is None else 'changed' if now != old else ''
        fingerprint = hashlib.sha256(json.dumps(now, sort_keys=True, default=str).encode()).hexdigest()
        lines.append(f'    %% {name} schema fingerprint: {fingerprint}')
        label = name + (f' ({status.upper()})' if status else '')
        lines.append(f'    {name}["{label}"] {{')
        columns = (now or old)['columns']
        old_columns = old['columns'] if old else {}
        for col in dict.fromkeys([*columns, *old_columns]):
            value = columns.get(col, old_columns.get(col))
            change = ('REMOVED' if now is None or col not in columns else
                      'ADDED' if col not in old_columns else
                      'CHANGED' if value != old_columns[col] else '')
            keys = ' ' + ','.join(value['keys']) if value['keys'] else ''
            note = 'nullable' if value['nullable'] else 'not null'
            if change:
                note = f'{change}; {note}'
            if value['default'] is not None:
                note += '; default: ' + str(value['default']).replace('"', "'")
            lines.append(f'        {value["type"]} {col}{keys} "{note}"')
        lines.append('    }')
        if status:
            lines.append(f'    class {name} {status}')
        prefix = name.split('_', 1)[0]
        for fk in (now or old)['foreign_keys']:
            parent = f'{prefix}_{fk["referred_table"]}'
            nullable = any(columns[col]['nullable'] for col in fk['constrained_columns'])
            cardinality = '|o' if nullable else '||'
            label = ', '.join(fk['constrained_columns'])
            if now is None:
                label += ' (REMOVED)'
            lines.append(f'    {parent} {cardinality}--o{{ {name} : "{label}"')
    logical = [('graph_runs', f'graph_{table}', 'run_id') for table in (
        'approvals', 'events', 'sessions', 'cli_node_executions', 'cli_run_submissions',
        'cli_loop_runs', 'cli_steering', 'github_comments', 'github_pull_requests',
    )]
    logical += [
        ('graph_cli_graphs', 'graph_cli_graph_versions', 'graph_id'),
        ('graph_cli_graph_versions', 'graph_cli_run_submissions', 'version_id'),
        ('graph_cli_graphs', 'graph_cli_loops', 'graph_id'),
        ('graph_cli_loops', 'graph_cli_loop_runs', 'loop_id'),
        ('graph_cli_node_executions', 'graph_cli_steering', 'execution_id'),
    ]
    for parent, child, column in logical:
        if parent in current and child in current and column in current[child]['columns']:
            lines.append(f'    {parent} |o..o{{ {child} : "{column} (logical)"')
    return '\n'.join(lines) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--base-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('docs/architecture'))
    parser.add_argument('--manifest', type=Path, default=Path('diagram-changes.json'))
    args = parser.parse_args()
    content = diagram(schemas(args.root), schemas(args.base_root))
    target = args.output / 'schema.mermaid'
    png = args.output / 'schema.png'
    changed = not target.exists() or target.read_text() != content or not png.exists()
    if changed:
        args.output.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    # PNG is rendered by Mermaid CLI only when the semantic diagram changes.
    args.manifest.write_text(json.dumps(
        [str(target), str(png)] if changed else []
    ))


if __name__ == '__main__':
    main()
