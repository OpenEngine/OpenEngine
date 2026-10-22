"""Validate against the authoritative contracts, bundled into installed wheels."""
import json
from pathlib import Path
from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from virtual_control_plane.domain.lifecycle import Rejected


class Contracts:
    def __init__(self):
        service = Path(__file__).resolve().parents[2]
        root = service / 'contracts'
        if not root.exists():
            root = service.parents[1] / 'packages' / 'contracts'
        self.root = root
        self.registry = Registry().with_resources([
            (path.resolve().as_uri(), Resource.from_contents(json.loads(path.read_text()), default_specification=DRAFT7))
            for path in root.rglob('*.schema.json')
        ])

    def validate(self, group, definition, value):
        if group == 'types':
            path = self.root / 'common/v1/types.schema.json'
        else:
            path = self.root / f'virtual_control_plane/v1/schemas/{group}.schema.json'
        ref = path.resolve().as_uri() + '#/$defs/' + definition
        errors = list(Draft7Validator({'$ref': ref}, registry=self.registry, format_checker=FormatChecker()).iter_errors(value))
        if errors:
            raise Rejected('INVALID_ARGUMENT', 'Message does not match the contract', violations=[
                {'field': '/' + '/'.join(str(part) for part in error.path), 'description': error.message[:256]}
                for error in errors[:5]])

    def route(self, subject):
        prefix = 'oe.v1.vcp.'
        if not subject.startswith(prefix):
            raise Rejected('INVALID_ARGUMENT', 'Unknown subject')
        parts = subject[len(prefix):].split('.')
        if parts == ['control', 'workspaces', 'create']:
            return 'command', 'create', None, 'create_workspace_command'
        if len(parts) == 4 and parts[:2] == ['control', 'workspaces']:
            self.validate('types', 'workspace_id', parts[2])
            action = parts[3]
            if action in ('ensure', 'execute', 'steer', 'cancel', 'checkpoint', 'suspend', 'destroy'):
                schema = f'{action}_workspace_command' if action == 'ensure' else f'{action}_command'
                return 'command', action, parts[2], schema
        if len(parts) == 5 and parts[:2] == ['query', 'workspaces'] and parts[3:] == ['sessions', 'list']:
            self.validate('types', 'workspace_id', parts[2])
            return 'query', 'sessions', parts[2], 'list_sessions_query'
        if len(parts) == 5 and parts[:2] == ['query', 'sessions'] and parts[3:] == ['events', 'list']:
            self.validate('types', 'session_id', parts[2])
            return 'query', 'session_events', parts[2], 'list_session_events_query'
        if parts == ['query', 'capabilities', 'get']:
            return 'query', 'capabilities', None, 'get_capabilities_query'
        if parts == ['query', 'workspaces', 'list']:
            return 'query', 'workspaces', None, 'list_workspaces_query'
        if len(parts) == 4 and parts[0] == 'query' and parts[3] == 'get' and parts[1] in ('workspaces', 'operations', 'sessions'):
            kind = parts[1][:-1]
            self.validate('types', kind + '_id', parts[2])
            return 'query', kind, parts[2], f'get_{kind}_query'
        raise Rejected('INVALID_ARGUMENT', 'Unknown or unimplemented subject')

    def command_id(self, value):
        try:
            self.validate('types', 'command_id', value)
        except Rejected:
            return False
        return True
