"""Workspace policy, independent of transports and persistence."""
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def identity(prefix: str) -> str:
    return f'{prefix}_{uuid4().hex}'


class Rejected(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.error = {'code': code, 'message': message, 'retryable': code == 'UNAVAILABLE'}
        if details:
            self.error['details'] = details


def unsupported(capability: str, provider: str | None = None):
    details = {'capability': capability}
    if provider:
        details['provider'] = provider
    raise Rejected('UNSUPPORTED_CAPABILITY', f'Unsupported capability: {capability}', **details)


def validate_create(payload: dict, capabilities: dict) -> None:
    supported = {f"isolation_{capabilities['isolation']}"}
    supported.update(key for key in ('checkpoint', 'suspend') if capabilities[key])
    if capabilities['restore'] != 'none':
        supported.add('restore_same_host')
    if capabilities['restore'] == 'any_host':
        supported.add('restore_any_host')
    for capability in payload.get('required_capabilities', []):
        if capability not in supported:
            unsupported(capability, payload['provider'])
    for tool in payload.get('tools', []):
        if tool not in capabilities['tools']:
            unsupported(f'tool.{tool}', payload['provider'])
    for key, value in payload.get('resources', {}).items():
        if key not in capabilities['max_resources'] or value > capabilities['max_resources'][key]:
            unsupported(f'resources.{key}', payload['provider'])
    for file in payload.get('context', {}).get('files', []):
        path = PurePosixPath(file['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts or '\x00' in file['path']:
            raise Rejected('INVALID_ARGUMENT', 'Context files must stay within the workspace')


def check_action(action: str, workspace: dict, payload: dict) -> None:
    state = workspace['state']
    if action == 'ensure' and state == 'suspended' and payload.get('restore', 'allow') == 'allow':
        unsupported('restore_same_host', workspace['provider'])
    allowed = {'ensure': {'ready'}, 'destroy': {'ready', 'failed', 'suspended', 'destroyed'}}
    if action not in allowed:
        unsupported(action, workspace['provider'])
    if state not in allowed[action] or (action == 'destroy' and workspace['active_attempt_ids']):
        raise Rejected('INVALID_STATE', f'Cannot {action} this workspace', resource_type='workspace',
                       resource_id=workspace['workspace_id'], current_state=state,
                       allowed_states=sorted(allowed[action]))
    if action == 'destroy' and set(payload.get('retention', {})) != {'artifacts', 'checkpoints'}:
        raise Rejected('INVALID_ARGUMENT', 'Destruction requires explicit retention choices')


def event(workspace: dict, operation: dict, request: dict, kind: str, payload: dict) -> dict:
    event_id = identity('evt')
    resources = {'workspace_id': workspace['workspace_id']}
    if 'runtime' in workspace:
        resources['runtime_id'] = workspace['runtime']['runtime_id']
    return {'event_id': event_id, 'event_type': kind, 'occurred_at': now(), 'producer': 'vcp',
            'causal_path': [*request['causal_path'], event_id], 'resources': resources,
            'resource_version': workspace['version'],
            'payload': {'operation': {key: operation[key] for key in ('operation_id', 'command_id')}, **payload}}
