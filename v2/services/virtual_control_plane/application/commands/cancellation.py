"""Accept cancellation without releasing execution ownership."""
from virtual_control_plane.domain.execution import transition
from virtual_control_plane.domain.lifecycle import Rejected, identity

TERMINAL = ('completed', 'failed', 'cancelled')


async def accept_cancel(tx, record, request, actor, timestamp, encoder):
    workspace = record['workspace']
    attempt_id = request['payload']['attempt_id']
    item = await tx.get('attempts', attempt_id)
    bound = await tx.get('attempt_bindings', attempt_id)
    if not item or not bound or bound['workspace_id'] != workspace['workspace_id']:
        raise Rejected('NOT_FOUND', 'Attempt does not exist in this workspace')
    attempt = item['attempt']
    terminal = attempt['state'] in TERMINAL
    runtime = workspace.get('runtime', {})
    ownership = await tx.get('runtime_ownership', workspace['workspace_id'])
    if not terminal and (not ownership or ownership['runtime_id'] != bound['runtime_id']
                         or ownership['generation'] != bound['runtime_generation']
                         or workspace['state'] != 'ready' or runtime.get('state') != 'running'
                         or runtime.get('runtime_id') != bound['runtime_id']
                         or runtime.get('generation') != bound['runtime_generation']):
        raise Rejected('INVALID_STATE', 'Attempt runtime is not current and running',
                       resource_type='workspace', resource_id=workspace['workspace_id'],
                       current_state=workspace['state'], allowed_states=['ready'])
    operation_id = identity('op')
    operation = {'operation_id': operation_id, 'command_id': request['command_id'],
                 'operation': 'workspaces.cancel', 'status': 'succeeded' if terminal else 'pending',
                 'resources': {key: bound[key] for key in ('workspace_id', 'runtime_id', 'session_id', 'attempt_id')},
                 'created_at': timestamp, 'updated_at': timestamp}
    if terminal:
        operation['result'] = {'attempt_id': attempt_id, 'final_state': attempt['state']}
    else:
        if encoder is None:
            raise Rejected('UNAVAILABLE', 'Runtime command delivery is not configured')
        execute = await tx.get('runtime_outbox', bound['command_id'])
        command_id = identity('rtc')
        message = {'command_id': command_id, 'kind': 'cancel', 'target': execute['message']['target'],
                   'execution': execute['message']['execution'], 'cause': {
                       'operation': {'operation_id': operation_id, 'command_id': request['command_id']},
                       'causal_path': request['causal_path']},
                   'payload': {key: request['payload'][key] for key in ('reason',) if key in request['payload']}}
        wire = encoder.encode_cancel(message)
        await tx.put('runtime_outbox', command_id, {'message': message, 'wire': wire.decode('utf-8'),
                     'acknowledged': False, 'subject': f"oe.internal.v1.runtime.runtimes.{bound['runtime_id']}.cancel"})
        if attempt['state'] != 'cancelling':
            item['attempt'] = transition(attempt, 'cancelling', timestamp=timestamp)
            await tx.put('attempts', attempt_id, item)
            workspace.update(version=workspace['version'] + 1, updated_at=timestamp)
            await tx.put('workspace_records', workspace['workspace_id'], record)
    await tx.put('operations', operation_id, {'owner': actor, 'operation': operation, 'request': request})
    return {'command_id': request['command_id'], 'status': 'accepted', 'operation_id': operation_id, 'resource_id': attempt_id}


async def complete_cancels(tx, attempt_id, state, timestamp):
    for record in await tx.all('operations'):
        operation = record['operation']
        if (operation['operation'] == 'workspaces.cancel' and operation['status'] == 'pending'
                and operation['resources'].get('attempt_id') == attempt_id):
            operation.update(status='succeeded', updated_at=timestamp, result={'attempt_id': attempt_id, 'final_state': state})
            await tx.put('operations', operation['operation_id'], record)
