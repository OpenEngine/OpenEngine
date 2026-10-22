"""Bind session steering to one running attempt at durable acceptance."""
from virtual_control_plane.domain.lifecycle import Rejected, identity, unsupported


async def accept_steer(tx, record, request, actor, timestamp, encoder):
    workspace = record['workspace']
    session_id = request['payload']['session_id']
    stored = await tx.get('sessions', session_id)
    if not stored or stored['session']['workspace_id'] != workspace['workspace_id']:
        raise Rejected('NOT_FOUND', 'Session does not exist in this workspace')
    session = stored['session']
    if workspace['state'] == 'suspended' and request['payload'].get('restore', 'allow') == 'allow':
        unsupported('restore')
    runtime = workspace.get('runtime', {})
    ownership = await tx.get('runtime_ownership', workspace['workspace_id'])
    if (workspace['state'] != 'ready' or runtime.get('state') != 'running' or not ownership
            or runtime.get('runtime_id') != ownership['runtime_id']
            or runtime.get('generation') != ownership['generation']
            or session['runtime_generation'] != runtime.get('generation')):
        raise Rejected('INVALID_STATE', 'Steering requires the current running runtime',
                       resource_type='workspace', resource_id=workspace['workspace_id'],
                       current_state=workspace['state'], allowed_states=['ready'])
    if session['state'] == 'idle':
        unsupported('steer.idle')
    attempts = [row for row in await tx.all('attempts') if row['attempt']['session_id'] == session_id
                and row['attempt']['state'] in ('starting', 'running', 'cancelling')]
    if session['state'] != 'active' or len(attempts) != 1 or attempts[0]['attempt']['state'] != 'running':
        raise Rejected('INVALID_STATE', 'Steering requires one running attempt',
                       resource_type='session', resource_id=session_id,
                       current_state=session['state'], allowed_states=['active'])
    status = await tx.get('runtime_status', runtime['runtime_id'])
    if not status or not status.get('ready') or status.get('quarantined'):
        raise Rejected('UNAVAILABLE', 'Runtime is not ready for steering')
    if encoder is None or session['agent']['runner'] not in status.get('steering_executors', []):
        unsupported('steer.active.' + session['agent']['runner'])
    attempt_id = attempts[0]['attempt']['attempt_id']
    bound = await tx.get('attempt_bindings', attempt_id)
    execution = (await tx.get('runtime_outbox', bound['command_id']))['message']
    operation_id, command_id = identity('op'), identity('rtc')
    operation = {'operation_id': operation_id, 'command_id': request['command_id'], 'operation': 'workspaces.steer',
                 'status': 'pending', 'resources': {key: value for key, value in execution['target'].items() if key.endswith('_id')},
                 'created_at': timestamp, 'updated_at': timestamp}
    message = {'command_id': command_id, 'kind': 'steer', 'target': execution['target'], 'execution': execution['execution'],
               'cause': {'operation': {'operation_id': operation_id, 'command_id': request['command_id']},
                         'causal_path': request['causal_path']}, 'payload': {'message': request['payload']['message']}}
    wire = encoder.encode_steer(message)
    await tx.put('operations', operation_id, {'owner': actor, 'operation': operation, 'request': request})
    await tx.put('runtime_outbox', command_id, {'message': message, 'wire': wire.decode('utf-8'), 'acknowledged': False,
                 'subject': f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.steer"})
    return {'command_id': request['command_id'], 'status': 'accepted', 'operation_id': operation_id, 'resource_id': session_id}
