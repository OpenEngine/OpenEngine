"""Stateless checks for the internal runtime contract; no transport implementation."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / 'internal/workspace_runtime/v1'
MAX_WIRE_BYTES = 1024 * 1024
DEFINITIONS = {
    'execute': ('runtime', ('execute',)),
    'cancel': ('runtime', ('cancel',)),
    'steer': ('runtime', ('steer',)),
    'command_ack': ('runtime', ('execute', 'cancel', 'steer')),
    'inspect': ('runtime', ('inspect',)),
    'inspect_response': ('runtime', ('inspect',)),
    **{name: ('vcp', ('updates',)) for name in (
        'update', 'ready', 'started', 'progress', 'completed', 'failed', 'cancelled', 'steering_applied', 'steering_failed', 'update_ack')},
}


def validator(definition):
    registry = Registry().with_resources([
        (path.resolve().as_uri(), Resource.from_contents(json.loads(path.read_text()), default_specification=DRAFT7))
        for path in ROOT.rglob('*.schema.json')
    ])
    return Draft7Validator({'$ref': (CONTRACT / 'messages.schema.json').as_uri() + '#/$defs/' + definition},
                           registry=registry, format_checker=FormatChecker())


def validate_wire_message(definition, data: bytes, subject):
    """Validate received NATS payload bytes before decoding or parsing JSON."""
    if len(data) > MAX_WIRE_BYTES:
        return ['Message exceeds 1 MiB']
    try:
        payload = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ['Message must be valid UTF-8 JSON']
    return validate_message(definition, payload, subject)


def validate_message(definition, payload, subject):
    """Check a decoded body; transport callers must use validate_wire_message."""
    errors = [error.message for error in validator(definition).iter_errors(payload)]
    if errors:
        return errors
    service, actions = DEFINITIONS[definition]
    expected = [f"oe.internal.v1.{service}.runtimes.{payload['target']['runtime_id']}.{action}" for action in actions]
    if subject not in expected:
        errors.append('Subject does not match message kind and runtime identity')
    for cause in [payload.get('cause'), payload.get('execution', {}).get('cause')]:
        if cause and cause['causal_path'][-1] != cause['operation']['command_id']:
            errors.append('Cause path must end with its public command ID')
    if definition == 'execute' and payload['execution']['command_id'] != payload['command_id']:
        errors.append('Execute identity must equal its command ID')
    if definition in ('cancel', 'steer') and payload['execution']['command_id'] == payload['command_id']:
        errors.append('Control command needs a distinct command ID')
    if payload.get('kind') == 'attempt.progress':
        progress = payload['payload']
        if progress['kind'] == 'artifact':
            for key in ('workspace_id', 'attempt_id'):
                if progress['artifact'].get(key) != payload['target'][key]:
                    errors.append(f'Artifact {key} differs from attempt binding')
        if progress['kind'] == 'usage' and datetime.fromisoformat(progress['period_start'].replace('Z', '+00:00')) > datetime.fromisoformat(progress['period_end'].replace('Z', '+00:00')):
            errors.append('Usage period ends before it starts')
    if definition == 'execute':
        context = payload['payload']['context']
        for value in [context['working_directory'], *context['instruction_files']]:
            if value.startswith('/') or '..' in value.split('/') or '\\' in value or '\x00' in value:
                errors.append('Context paths must stay relative to the workspace')
    if definition == 'inspect_response' and payload['status'] == 'ok':
        result = payload['result']
        seen = set()
        for snapshot in result['attempts']:
            if snapshot['attempt_id'] in seen:
                errors.append('Duplicate attempt snapshot')
            seen.add(snapshot['attempt_id'])
            if snapshot['known']:
                if snapshot['attempt_id'] != snapshot['target']['attempt_id']:
                    errors.append('Snapshot attempt identity mismatch')
                if binding_errors(snapshot, payload['target']):
                    errors.append('Snapshot runtime binding mismatch')
                if snapshot['last_event_sequence'] > result['last_event_sequence']:
                    errors.append('Snapshot exceeds journal high-water sequence')
    return errors


def binding_errors(payload, provisioned_binding):
    """Compare wire claims to independently provisioned ownership, never authenticate them."""
    return [f'Binding mismatch: {key}' for key, value in provisioned_binding.items()
            if payload['target'].get(key) != value]


def check():
    errors = []
    for path in sorted((CONTRACT / 'examples').glob('*.json')):
        case = json.loads(path.read_text())
        errors.extend(f'{path.name}: {error}' for error in validate_wire_message(
            case['definition'], json.dumps(case['payload'], ensure_ascii=False).encode('utf-8'),
            case.get('subject', case.get('in_reply_to'))))
    return errors


if __name__ == '__main__':
    problems = check()
    print('\n'.join(problems) if problems else 'Internal workspace runtime contract examples valid')
    raise SystemExit(bool(problems))
