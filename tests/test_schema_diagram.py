"""Comparison diagrams retain deletions without presenting them as current objects."""
from copy import deepcopy

from scripts.schema_diagram import diagram, schemas
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_diagram_tracks_added_removed_and_changed_objects():
    previous = schemas(ROOT)
    current = deepcopy(previous)
    removed = current.pop('state_messages')
    current['state_new_messages'] = removed
    columns = current['state_agent_instances']['columns']
    columns.pop('title')
    columns['new_column'] = {'type': 'TEXT', 'nullable': True, 'default': None, 'keys': []}
    columns['runner']['nullable'] = True
    output = diagram(current, previous)
    assert 'class state_messages removed' in output
    assert 'class state_new_messages added' in output
    assert 'class state_agent_instances changed' in output
    assert 'title "REMOVED;' in output
    assert 'new_column "ADDED;' in output
    assert 'runner "CHANGED;' in output
    assert diagram(current, previous) == output


def test_unchanged_schema_has_no_highlights():
    current = schemas(ROOT)
    output = diagram(current, current)
    assert '    class ' not in output
    assert 'alembic_version' not in output
    assert output.count(' {\n') == len(current)
