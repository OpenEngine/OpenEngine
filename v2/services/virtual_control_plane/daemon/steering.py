"""Durable active-attempt steering for transactionally applied executors."""
from virtual_control_plane.domain.lifecycle import now


def failure(message, code='PROVIDER_ERROR', **details):
    result = {'code': code, 'message': message, 'retryable': False}
    if details:
        result['details'] = details
    return result


class SteeringWorker:
    def __init__(self, journal):
        self.journal = journal

    async def run_once(self):
        # This first capability is transactional: executor effects and outcome
        # commit together. An external executor must supply durable idempotency
        # and turn fencing before advertising this capability.
        async with self.journal.storage.transaction() as tx:
            rows = [row for row in await tx.all('daemon_commands') if row.get('state') == 'steer_pending']
            if not rows:
                return False
            row = min(rows, key=lambda row: (row['accepted_at'], row['command']['command_id']))
            command = row['command']
            attempt = await tx.get('daemon_attempts', command['target']['attempt_id'])
            error = None
            if (not attempt or attempt['command']['target'] != command['target']
                    or attempt['command']['execution'] != command['execution'] or attempt['state'] != 'running'):
                error = failure('The bound attempt is no longer running', 'INVALID_STATE',
                                resource_type='attempt', resource_id=command['target']['attempt_id'],
                                current_state=attempt['state'] if attempt else 'unknown', allowed_states=['running'])
            else:
                backend = self.journal.backends.get(attempt['command']['payload']['agent']['runner'])
                if not backend or not getattr(backend, 'supports_steering', False):
                    error = failure('Executor does not support safe active steering', 'UNSUPPORTED_CAPABILITY', capability='steer.active')
                else:
                    await backend.apply_steer(tx, command)
            kind = 'steering.failed' if error else 'steering.applied'
            sequence = await self.journal.append(tx, kind, {'error': error} if error else {}, command, steer=command)
            row.update(state='steer_done', completed_at=now(), last_event_sequence=sequence)
            await tx.put('daemon_commands', command['command_id'], row)
        return True
