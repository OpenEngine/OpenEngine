"""Retry immutable commands until the daemon acknowledges durable ownership."""
import nats
from virtual_control_plane.domain.runtime import invalid
from virtual_control_plane.application.runtime_updates import project
from virtual_control_plane.domain.lifecycle import now
from virtual_control_plane.transport.nats.runtime_protocol import Backoff


class RuntimeDispatcher:
    def __init__(self, storage, transport):
        self.storage, self.transport = storage, transport
        self.backoff = Backoff()

    async def run_once(self):
        async with self.storage.transaction() as tx:
            pending = [row for row in await tx.all('runtime_outbox') if not row['acknowledged'] and not row.get('blocked')
                       and self.backoff.due(row['message']['command_id'])]
        if not pending:
            return False
        row = pending[0]
        command = row['message']
        key = command['command_id']
        try:
            ack = await self.transport.request(row['subject'], row['wire'].encode('utf-8'), 'command_ack')
            if ack['command_id'] != key or ack['target'] != command['target']:
                invalid('Acknowledgement identity mismatch')
            if ack['status'] == 'rejected' and ack['error']['retryable']:
                self.backoff.failed(key)
                return False
        except (TimeoutError, OSError, ValueError, nats.errors.Error):
            self.backoff.failed(key)
            return False
        async with self.storage.transaction() as tx:
            current = await tx.get('runtime_outbox', key)
            current['ack'] = ack
            if ack['status'] == 'accepted':
                current['acknowledged'] = True
            elif command['kind'] == 'steer':
                # A rejected steer cannot stop or fail the original execution.
                await project(tx, {**{field: command[field] for field in ('target', 'execution', 'cause')},
                              'steer_command_id': key, 'kind': 'steering.failed', 'occurred_at': now(),
                              'payload': {'error': {'code': 'PROVIDER_ERROR', 'message': ack['error']['message'], 'retryable': False}}})
                current['acknowledged'] = True
            elif command['kind'] == 'execute' and ack['error']['code'] in ('INVALID_ARGUMENT', 'UNSUPPORTED_EXECUTOR', 'SESSION_BUSY'):
                # These durable admission rejections establish no launch for this command.
                attempt = await tx.get('attempts', command['target']['attempt_id'])
                if attempt['attempt']['state'] not in ('completed', 'failed', 'cancelled'):
                    await project(tx, {'target': command['target'], 'execution': command['execution'],
                              'kind': 'attempt.failed', 'occurred_at': now(), 'payload': {'error': {
                                  'code': 'PROVIDER_ERROR', 'message': ack['error']['message'], 'retryable': False}}})
                current['acknowledged'] = True
            else:
                # Conflicting identities are not evidence that an old executor stopped.
                current['blocked'] = True
            await tx.put('runtime_outbox', key, current)
        self.backoff.done(key)
        return True
