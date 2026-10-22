from copy import deepcopy
import json

import pytest
import runtime_contract as runtime
import yaml


def example(name):
    return json.loads((runtime.CONTRACT / 'examples' / f'{name}.json').read_text())


def errors(case):
    return runtime.validate_message(case['definition'], case['payload'], case.get('subject', case.get('in_reply_to')))


def test_examples_and_all_message_schemas():
    assert runtime.check() == []
    document = yaml.safe_load((runtime.CONTRACT / 'asyncapi.yaml').read_text())
    definitions = json.loads((runtime.CONTRACT / 'messages.schema.json').read_text())['$defs']
    for message in document['components']['messages'].values():
        definition = message['payload']['schema']['$ref'].split('/')[-1]
        assert definition in definitions
    cases = [json.loads(path.read_text()) for path in (runtime.CONTRACT / 'examples').glob('*.json')]
    assert set(runtime.DEFINITIONS) - {'update'} <= {case['definition'] for case in cases}
    for case in cases:
        if case['definition'] in ('ready', 'started', 'progress', 'completed', 'failed', 'cancelled'):
            assert list(runtime.validator('update').iter_errors(case['payload'])) == []


def test_execute_retry_preserves_complete_identity():
    assert example('execute')['payload'] == example('execute-retry')['payload']


@pytest.mark.parametrize(('name', 'path', 'value'), [
    ('execute', ('target', 'runtime_generation'), 0),
    ('execute', ('target', 'attempt_id'), 'ws_wrong'),
    ('execute', ('execution', 'command_id'), 'different'),
    ('execute', ('execution', 'cause', 'causal_path'), ['wrong']),
    ('execute', ('execution', 'cause', 'causal_path'), [f'ancestor_{n}' for n in range(31)] + ['cmd_execute']),
    ('execute', ('payload', 'context', 'working_directory'), '../escape'),
    ('execute', ('payload', 'context', 'instruction_files'), ['/host/secret']),
    ('execute', ('payload', 'actor'), 'untrusted'),
    ('cancel', ('command_id',), 'rtc_execute'),
    ('accepted', ('error',), {'code':'UNAVAILABLE','message':'no','retryable':True}),
    ('stale-runtime', ('error', 'retryable'), True),
    ('completed', ('termination',), 'never_started'),
    ('completed', ('sequence',), 0),
    ('event-gap', ('error', 'retryable'), False),
    ('inspect-response', ('result', 'last_event_sequence'), 0),
])
def test_rejects_invalid_wire_variants(name, path, value):
    case = deepcopy(example(name))
    node = case['payload']
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert errors(case)


@pytest.mark.parametrize(('name','field'), [('cancelled','termination'), ('cancelled','cause'), ('event-gap','expected_sequence'), ('stale-runtime','error')])
def test_required_safety_fields(name, field):
    case = example(name)
    del case['payload'][field]
    assert errors(case)


def test_subject_and_provisioned_binding_checks():
    case = example('execute')
    case['subject'] = case['subject'].replace('rt_demo', 'rt_other')
    assert errors(case)
    payload = example('started')['payload']
    assert runtime.binding_errors(payload, {'workspace_id':'ws_demo','runtime_id':'rt_demo','runtime_generation':1}) == []
    assert runtime.binding_errors(payload, {'runtime_generation':2})
    assert runtime.binding_errors(payload, {'workspace_id':'ws_other'})


def test_inspect_response_must_bind_snapshot_to_runtime():
    case = example('inspect-response')
    case['payload']['result']['attempts'][0]['target']['runtime_generation'] = 2
    assert errors(case)


def test_progress_usage_and_artifact_bindings():
    case = example('progress')
    case['payload']['payload'] = {'kind':'usage','usage':{'input_tokens':1,'output_tokens':2},
                                 'period_start':'2026-10-22T12:00:00Z','period_end':'2026-10-22T12:00:01Z'}
    assert errors(case) == []
    case['payload']['payload']['period_end'] = '2026-10-22T11:00:00Z'
    assert errors(case)
    artifact_example = json.loads((runtime.ROOT / 'virtual_control_plane/v1/examples/artifact.created.event.json').read_text())
    artifact = artifact_example['payload']['payload']['artifact']
    artifact.update(workspace_id='ws_demo', attempt_id='attempt_demo')
    case['payload']['payload'] = {'kind':'artifact','artifact':artifact}
    assert errors(case) == []
    artifact['attempt_id'] = 'attempt_other'
    assert errors(case)


def test_internal_subjects_are_outside_public_streams():
    document = yaml.safe_load((runtime.CONTRACT / 'asyncapi.yaml').read_text())
    streams = runtime.ROOT.parents[1] / 'infra/nats/streams'
    for channel in document['channels'].values():
        address = channel['address']
        if address:
            assert address.startswith('oe.internal.v1.')
            for path in streams.glob('*.json'):
                for subject in json.loads(path.read_text())['subjects']:
                    assert not address.startswith(subject.removesuffix('>'))


def test_wire_limit_accepts_unicode_without_ascii_reserialization():
    case = example('progress')
    case['payload']['payload']['text'] = '😀' * 90_000
    wire = json.dumps(case['payload'], ensure_ascii=False).encode('utf-8')
    assert len(wire) < runtime.MAX_WIRE_BYTES
    assert len(json.dumps(case['payload']).encode('utf-8')) > runtime.MAX_WIRE_BYTES
    assert runtime.validate_wire_message('progress', wire, case['subject']) == []


def test_wire_limit_counts_whitespace_and_accepts_exact_boundary():
    case = example('progress')
    wire = json.dumps(case['payload']).encode('utf-8')
    padded = wire + b' ' * (runtime.MAX_WIRE_BYTES - len(wire))
    assert runtime.validate_wire_message('progress', padded, case['subject']) == []
    assert json.loads(padded + b' ') == case['payload']
    assert runtime.validate_wire_message('progress', padded + b' ', case['subject']) == ['Message exceeds 1 MiB']


def test_oversized_wire_is_rejected_before_decode_or_parse():
    case = example('progress')
    assert runtime.validate_wire_message('progress', b'\xff' * (runtime.MAX_WIRE_BYTES + 1), case['subject']) == ['Message exceeds 1 MiB']


@pytest.mark.parametrize('wire', [b'\xff', b'{invalid', b'\xff\xfe{\x00}\x00'])
def test_wire_requires_utf8_json(wire):
    assert runtime.validate_wire_message('progress', wire, example('progress')['subject']) == ['Message must be valid UTF-8 JSON']
