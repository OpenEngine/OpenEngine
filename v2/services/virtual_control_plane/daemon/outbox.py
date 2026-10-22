"""Deliver the oldest journal update, retaining exact bytes after acknowledgement."""
import nats
from virtual_control_plane.domain.runtime import invalid
from virtual_control_plane.transport.nats.runtime_protocol import Backoff
from virtual_control_plane.domain.runtime import binding


class UpdateOutbox:
    def __init__(self, journal, transport):
        self.journal, self.transport = journal, transport
        self.backoff = Backoff()

    async def run_once(self):
        async with self.journal.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            if meta.get('blocked'):
                return False
            rows = sorted((row for row in await tx.all('daemon_updates') if not row['acknowledged']), key=lambda row: row['message']['sequence'])
        if not rows:
            return False
        row = rows[0]
        message = row['message']
        key = message['event_id']
        if not self.backoff.due(key):
            return False
        try:
            ack = await self.transport.request(f"oe.internal.v1.vcp.runtimes.{self.journal.target['runtime_id']}.updates",
                                               row['wire'].encode('utf-8'), 'update_ack')
            if ack['event_id'] != key or ack['sequence'] != message['sequence'] or ack['target'] != binding(message['target']):
                invalid('Update acknowledgement identity mismatch')
        except (TimeoutError, OSError, ValueError, nats.errors.Error):
            self.backoff.failed(key)
            return False
        async with self.journal.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            if ack['status'] == 'committed':
                row['acknowledged'] = True
                await tx.put('daemon_updates', str(message['sequence']), row)
                if message['sequence'] == meta['ready_sequence']:
                    meta['ready'] = True
            elif ack['error']['code'] == 'EVENT_GAP':
                expected = ack['expected_sequence']
                saved = await tx.get('daemon_updates', str(expected))
                if expected > message['sequence'] or saved is None:
                    meta.update(blocked=True, ready=False)
                else:
                    for entry in await tx.all('daemon_updates'):
                        if entry['message']['sequence'] >= expected:
                            entry['acknowledged'] = False
                            await tx.put('daemon_updates', str(entry['message']['sequence']), entry)
            elif not ack['error']['retryable']:
                meta.update(blocked=True, ready=False, last_error=ack['error'])
            await tx.put('daemon_meta', 'runtime', meta)
        if ack['status'] == 'committed':
            self.backoff.done(key)
            return True
        self.backoff.failed(key)
        return False
