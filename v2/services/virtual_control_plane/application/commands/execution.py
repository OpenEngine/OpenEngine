"""Atomically accept execution; delivery and runtime updates belong to Step 3."""
from copy import deepcopy

from virtual_control_plane.domain.execution import new_attempt, new_session, transition
from virtual_control_plane.domain.lifecycle import Rejected, identity, unsupported


async def accept_execute(tx, record, request, actor, timestamp, context_port, encoder, authorizer, subject):
    workspace = record['workspace']
    workspace_id = workspace['workspace_id']
    runtime = workspace.get('runtime')
    ownership = await tx.get('runtime_ownership', workspace_id)
    if (workspace['state'] != 'ready' or not runtime or runtime['state'] != 'running'
            or not ownership or ownership['workspace_id'] != workspace_id
            or runtime['runtime_id'] != ownership['runtime_id']
            or runtime['generation'] != ownership['generation']):
        raise Rejected('INVALID_STATE', 'Execute requires a ready workspace with its current running runtime',
                       resource_type='workspace', resource_id=workspace_id,
                       current_state=workspace['state'], allowed_states=['ready'])
    payload = request['payload']
    selection = payload['session']
    if selection['mode'] == 'new':
        session = new_session(workspace_id, runtime['generation'], payload['agent'], timestamp)
    else:
        stored = await tx.get('sessions', selection['session_id'])
        if stored is None or stored['session']['workspace_id'] != workspace_id:
            raise Rejected('NOT_FOUND', 'Session does not exist in this workspace')
        await authorizer.authorize(actor, subject, stored['owner'])
        session = stored['session']
        if session['runtime_generation'] != runtime['generation'] or session['state'] != 'idle':
            raise Rejected('INVALID_STATE', 'Session must be idle in the current runtime generation',
                           resource_type='session', resource_id=session['session_id'],
                           current_state=session['state'], allowed_states=['idle'])
        if session['agent'] != payload['agent']:
            raise Rejected('INVALID_ARGUMENT', 'Continuing a session requires the same agent configuration')
    if context_port is None or encoder is None:
        unsupported('execute.runtime')
    if getattr(context_port, 'requires_registration', False):
        status = await tx.get('runtime_status', runtime['runtime_id'])
        if not status or not status.get('ready') or status.get('quarantined'):
            raise Rejected('UNAVAILABLE', 'Runtime has not registered execution readiness')
        if payload['agent']['runner'] not in status['executors']:
            unsupported('executor.' + payload['agent']['runner'])
    context = await context_port.prepare(deepcopy(record), deepcopy(runtime), deepcopy(payload))
    session = transition(session, 'active', timestamp=timestamp)
    attempt = new_attempt(session, payload['task']['instruction'])
    operation_id, runtime_command_id = identity('op'), identity('rtc')
    binding = {'workspace_id': workspace_id, 'runtime_id': runtime['runtime_id'],
               'runtime_generation': runtime['generation'], 'session_id': session['session_id'],
               'attempt_id': attempt['attempt_id']}
    operation = {'operation_id': operation_id, 'command_id': request['command_id'],
                 'operation': 'workspaces.execute', 'status': 'pending', 'resources': {key: value for key, value in binding.items() if key.endswith('_id')},
                 'created_at': timestamp, 'updated_at': timestamp}
    message = {'command_id': runtime_command_id, 'kind': 'execute', 'target': binding,
               'execution': {'command_id': runtime_command_id, 'cause': {
                   'operation': {'operation_id': operation_id, 'command_id': request['command_id']},
                   'causal_path': request['causal_path']}},
               'payload': {'agent': payload['agent'], 'task': payload['task'],
                           'session_mode': selection['mode'], 'context': context}}
    wire = encoder.encode_execute(message)
    # No acceptance can escape without every linked record. Storage rolls back
    # exceptions, including faults on the last write or receipt persistence.
    await tx.put('sessions', session['session_id'], {'owner': actor, 'session': session})
    await tx.put('attempts', attempt['attempt_id'], {'owner': actor, 'attempt': attempt,
                 'created_at': timestamp, 'operation_id': operation_id})
    await tx.put('attempt_bindings', attempt['attempt_id'], {**binding, 'command_id': runtime_command_id})
    await tx.put('runtime_outbox', runtime_command_id, {'message': message, 'wire': wire.decode('utf-8'), 'acknowledged': False,
                 'subject': f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.execute"})
    await tx.put('operations', operation_id, {'owner': actor, 'operation': operation, 'request': request})
    workspace['active_attempt_ids'].append(attempt['attempt_id'])
    workspace.update(version=workspace['version'] + 1, updated_at=timestamp)
    await tx.put('workspace_records', workspace_id, record)
    return {'command_id': request['command_id'], 'status': 'accepted',
            'operation_id': operation_id, 'resource_id': attempt['attempt_id']}
