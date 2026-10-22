"""Commit internal update identity, lifecycle state and public events together."""
from datetime import datetime
from virtual_control_plane.application.steering_updates import project_steering, fail_pending_steers
from virtual_control_plane.application.commands.cancellation import complete_cancels
from virtual_control_plane.domain.execution import transition
from virtual_control_plane.domain.lifecycle import Rejected, event, now
from virtual_control_plane.domain.runtime import binding, digest, error


class UpdateRejected(Exception):
    def __init__(self, code, message):
        self.detail = error(code, message)


def reject(code, message):
    raise UpdateRejected(code, message)


async def project(tx, message):
    if message['kind'] in ('steering.applied', 'steering.failed'):
        return await project_steering(tx, message, reject)
    target = message['target']
    item = await tx.get('attempts', target['attempt_id'])
    bound = await tx.get('attempt_bindings', target['attempt_id'])
    execution = message['execution']
    if not item or not bound or any(bound.get(key) != value for key, value in target.items()) or bound['command_id'] != execution['command_id']:
        reject('IDENTITY_MISMATCH', 'Unknown attempt or execution binding')
    record = await tx.get('operations', item['operation_id'])
    operation, request = record['operation'], record['request']
    expected = {'operation': {key: operation[key] for key in ('operation_id', 'command_id')}, 'causal_path': request['causal_path']}
    if execution['cause'] != expected:
        reject('IDENTITY_MISMATCH', 'Execute cause differs from acceptance')
    workspace_record = await tx.get('workspace_records', target['workspace_id'])
    workspace = workspace_record['workspace']
    session_record = await tx.get('sessions', target['session_id'])
    session, attempt = session_record['session'], item['attempt']
    kind, payload = message['kind'], message['payload']
    timestamp = message['occurred_at']
    history = None
    event_request = request
    if kind == 'attempt.cancelled':
        cancellation = await tx.get('runtime_outbox', message['cancel_command_id'])
        if (not cancellation or cancellation['message']['kind'] != 'cancel'
                or cancellation['message']['target'] != target
                or cancellation['message']['execution'] != execution
                or cancellation['message']['cause'] != message['cause']):
            reject('IDENTITY_MISMATCH', 'Unknown cancellation cause')
        event_request = {'causal_path': message['cause']['causal_path']}
    try:
        if kind == 'attempt.started':
            if attempt['state'] == 'cancelling' and 'started_at' not in attempt:
                attempt['started_at'] = timestamp
            else:
                attempt = transition(attempt, 'running', timestamp=timestamp)
            operation.update(status='succeeded', updated_at=timestamp, result={
                'attempt_id': target['attempt_id'], 'session_id': target['session_id'],
                'runtime_generation': target['runtime_generation'], 'restored': False})
            public_kind = 'task.started'
            public_payload = {'runtime_generation': target['runtime_generation'], 'session_mode': request['payload']['session']['mode']}
        elif kind in ('attempt.completed', 'attempt.failed', 'attempt.cancelled'):
            state = kind.split('.')[1]
            attempt = transition(attempt, state, timestamp=timestamp)
            session = transition(session, 'idle', timestamp=timestamp)
            workspace['active_attempt_ids'].remove(target['attempt_id'])
            public_kind = 'task.' + state
            public_payload = {key: value for key, value in payload.items() if key in ('result', 'error')}
            attempt.update(public_payload)
            if state == 'cancelled':
                public_payload = {**payload, 'cancelled_by': message['cause']['operation']}
            if operation['status'] == 'pending':
                detail = payload.get('error') or {'code': 'INVALID_STATE', 'message': 'Attempt cancelled before starting',
                    'retryable': False, 'details': {'resource_type': 'attempt', 'resource_id': target['attempt_id'],
                                                  'current_state': 'cancelled', 'allowed_states': ['running']}}
                operation.update(status='failed', updated_at=timestamp, error=detail)
            await complete_cancels(tx, target['attempt_id'], state, timestamp)
        elif kind == 'attempt.progress' and payload['kind'] == 'conversation':
            if attempt['state'] not in ('running', 'cancelling') or 'started_at' not in attempt:
                reject('EVENT_CONFLICT', 'Progress requires a running attempt')
            history_key = target['session_id'] + ':' + payload['message_id']
            if await tx.get('session_history', history_key):
                reject('EVENT_CONFLICT', 'Conversation message identity already used')
            entries = [row for row in await tx.all('session_history') if row['session_id'] == target['session_id']]
            sequence = max((row['sequence'] for row in entries), default=0) + 1
            history = {'session_id': target['session_id'], 'attempt_id': target['attempt_id'],
                       'message_id': payload['message_id'], 'sequence': sequence, 'role': payload['role'],
                       'text': payload['text'], 'occurred_at': timestamp}
            public_kind = 'conversation.message'
            public_payload = {key: history[key] for key in ('role', 'text', 'sequence')}
        elif kind == 'attempt.progress' and payload['kind'] == 'usage':
            if attempt['state'] not in ('running', 'cancelling') or 'started_at' not in attempt:
                reject('EVENT_CONFLICT', 'Usage requires a running attempt')
            start, end = (datetime.fromisoformat(payload[key].replace('Z', '+00:00')) for key in ('period_start', 'period_end'))
            periods = item.get('usage_periods', [])
            if start > end or any(start < datetime.fromisoformat(period[1].replace('Z', '+00:00'))
                                  and end > datetime.fromisoformat(period[0].replace('Z', '+00:00')) for period in periods):
                reject('EVENT_CONFLICT', 'Usage delta periods overlap or are reversed')
            item['usage_periods'] = [*periods, [payload['period_start'], payload['period_end']]]
            total = attempt.setdefault('usage', {})
            for key, value in payload['usage'].items():
                total[key] = total.get(key, 0) + value
            public_kind = 'usage.reported'
            public_payload = {key: payload[key] for key in ('usage', 'period_start', 'period_end')}
        else:
            reject('EVENT_CONFLICT', 'Unsupported update kind for this executor milestone')
    except (Rejected, ValueError) as exc:
        reject('EVENT_CONFLICT', str(exc))
    workspace.update(version=workspace['version'] + 1, updated_at=now())
    outgoing = event(workspace, operation, event_request, public_kind, public_payload)
    outgoing['occurred_at'] = timestamp
    outgoing['resources'].update(session_id=target['session_id'], attempt_id=target['attempt_id'])
    if public_kind == 'usage.reported':
        outgoing['payload'].pop('operation')
    if history:
        outgoing['payload'].pop('operation')
        outgoing['resources']['message_id'] = payload['message_id']
        await tx.put('session_history', history_key, history)
    item['attempt'], session_record['session'] = attempt, session
    await tx.put('attempts', target['attempt_id'], item)
    await tx.put('sessions', target['session_id'], session_record)
    await tx.put('operations', operation['operation_id'], record)
    await tx.put('workspace_records', target['workspace_id'], workspace_record)
    await tx.put('outbox', outgoing['event_id'], {'event': outgoing, 'published': False})
    if kind in ('attempt.completed', 'attempt.failed', 'attempt.cancelled'):
        await fail_pending_steers(tx, target, timestamp, reject)


class RuntimeUpdates:
    def __init__(self, storage):
        self.storage = storage

    async def accept(self, subject, message):
        target = binding(message['target'])
        runtime_id = target['runtime_id']
        response = {'event_id': message['event_id'], 'target': target, 'sequence': message['sequence'], 'status': 'committed'}
        async with self.storage.transaction() as tx:
            status = await tx.get('runtime_status', runtime_id)
            try:
                ownership = await tx.get('runtime_ownership', target['workspace_id'])
                workspace = await tx.get('workspace_records', target['workspace_id'])
                if not ownership or ownership['runtime_id'] != runtime_id or subject != f'oe.internal.v1.vcp.runtimes.{runtime_id}.updates':
                    reject('IDENTITY_MISMATCH', 'Unknown runtime binding')
                if (ownership['generation'] == target['runtime_generation']
                        and workspace['workspace']['state'] == 'provisioning'
                        and message['kind'] == 'runtime.ready'):
                    reject('UNAVAILABLE', 'Workspace provisioning has not committed yet')
                runtime = workspace['workspace'].get('runtime', {})
                if (ownership['generation'] != target['runtime_generation'] or workspace['workspace']['state'] != 'ready'
                        or runtime.get('runtime_id') != runtime_id or runtime.get('generation') != target['runtime_generation']
                        or runtime.get('state') != 'running'):
                    reject('STALE_RUNTIME', 'Runtime is not current and ready')
                previous = await tx.get('runtime_updates', message['event_id'])
                if previous:
                    if previous['digest'] != digest(message):
                        reject('EVENT_CONFLICT', 'Event identity changed')
                    return response
                if status and status.get('quarantined'):
                    reject('EVENT_CONFLICT', 'Runtime requires reconciliation')
                expected = (status['sequence'] if status else 0) + 1
                if message['sequence'] < expected:
                    reject('EVENT_CONFLICT', 'Sequence already occupied')
                if message['sequence'] > expected:
                    response['expected_sequence'] = expected
                    reject('EVENT_GAP', 'Replay missing events first')
                if message['kind'] == 'runtime.ready':
                    if status and status['journal_id'] != message['payload']['journal_id']:
                        reject('IDENTITY_MISMATCH', 'Runtime journal was replaced')
                    status = {**(status or {}), **message['payload'], 'ready': True,
                              'steering_executors': message['payload'].get('steering_executors', [])}
                else:
                    if not status:
                        reject('IDENTITY_MISMATCH', 'Runtime must register its journal first')
                    await project(tx, message)
                status.update(sequence=message['sequence'], boot_id=message['boot_id'], target=target)
                await tx.put('runtime_status', runtime_id, status)
                await tx.put('runtime_updates', message['event_id'], {'digest': digest(message), 'message': message})
            except UpdateRejected as exc:
                response.update(status='rejected', error=exc.detail)
                if status and exc.detail['code'] == 'EVENT_CONFLICT':
                    status.update(quarantined=True, ready=False)
                    await tx.put('runtime_status', runtime_id, status)
        return response
