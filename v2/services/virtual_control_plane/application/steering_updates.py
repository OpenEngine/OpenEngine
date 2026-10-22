"""Project confirmed steering delivery separately from attempt termination."""
from virtual_control_plane.domain.lifecycle import event, now


async def project_steering(tx, message, reject):
    command = await tx.get('runtime_outbox', message['steer_command_id'])
    if (not command or command['message']['kind'] != 'steer'
            or any(command['message'][key] != message[key] for key in ('target', 'execution', 'cause'))):
        reject('IDENTITY_MISMATCH', 'Unknown steering binding or cause')
    command = command['message']
    record = await tx.get('operations', command['cause']['operation']['operation_id'])
    operation = record['operation']
    if operation['status'] != 'pending':
        if record.get('steering_terminal_resolution') and message['kind'] == 'steering.failed':
            return
        reject('EVENT_CONFLICT', 'Steering already has a terminal outcome')
    target, timestamp = message['target'], message['occurred_at']
    state = message['kind'].split('.')[1]
    payload = {}
    if state == 'applied':
        operation.update(status='succeeded', result={'session_id': target['session_id'],
                         'attempt_id': target['attempt_id'], 'runtime_generation': target['runtime_generation'], 'restored': False})
        payload['runtime_generation'] = target['runtime_generation']
    else:
        operation.update(status='failed', error=message['payload']['error'])
        payload['error'] = message['payload']['error']
    operation['updated_at'] = timestamp
    workspace_record = await tx.get('workspace_records', target['workspace_id'])
    workspace = workspace_record['workspace']
    workspace.update(version=workspace['version'] + 1, updated_at=now())
    outgoing = event(workspace, operation, record['request'], message['kind'], payload)
    outgoing['occurred_at'] = timestamp
    outgoing['resources'].update(session_id=target['session_id'], attempt_id=target['attempt_id'])
    entries = [row for row in await tx.all('session_history') if row['session_id'] == target['session_id']]
    history = {'session_id': target['session_id'], 'attempt_id': target['attempt_id'],
               'sequence': max((row['sequence'] for row in entries), default=0) + 1,
               'occurred_at': timestamp, 'kind': 'steering', 'message': command['payload']['message'],
               'status': state, 'operation_id': operation['operation_id']}
    await tx.put('session_history', target['session_id'] + ':' + operation['operation_id'], history)
    await tx.put('operations', operation['operation_id'], record)
    await tx.put('workspace_records', target['workspace_id'], workspace_record)
    await tx.put('outbox', outgoing['event_id'], {'event': outgoing, 'published': False})


async def fail_pending_steers(tx, target, timestamp, reject):
    # A steering effect and its update are atomic in this protocol revision.
    # Therefore any applied outcome precedes the attempt's terminal sequence.
    for row in await tx.all('runtime_outbox'):
        command = row['message']
        if command['kind'] != 'steer' or command['target'] != target:
            continue
        operation_id = command['cause']['operation']['operation_id']
        record = await tx.get('operations', operation_id)
        if record['operation']['status'] != 'pending':
            continue
        await project_steering(tx, {**{key: command[key] for key in ('target', 'execution', 'cause')},
            'steer_command_id': command['command_id'], 'kind': 'steering.failed', 'occurred_at': timestamp,
            'payload': {'error': {'code': 'INVALID_STATE', 'message': 'Attempt ended before steering was applied',
                'retryable': False, 'details': {'resource_type': 'attempt', 'resource_id': target['attempt_id'],
                                               'current_state': 'terminal', 'allowed_states': ['running']}}}}, reject)
        record = await tx.get('operations', operation_id)
        record['steering_terminal_resolution'] = True
        await tx.put('operations', operation_id, record)
