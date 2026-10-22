"""In-process executor used by deterministic runtime tests."""
import asyncio
from virtual_control_plane.domain.lifecycle import identity
from virtual_control_plane.domain.runtime import invalid


class FakeBackend:
    progress_kinds = ('conversation',)
    supports_steering = True

    async def apply_steer(self, tx, command):
        attempt = await tx.get('daemon_attempts', command['target']['attempt_id'])
        attempt.setdefault('steering', {})[command['command_id']] = command['payload']['message']
        await tx.put('daemon_attempts', command['target']['attempt_id'], attempt)

    def validate(self, payload):
        settings = payload['agent'].get('settings', {})
        if set(settings) - {'outcome', 'delay_seconds'} or settings.get('outcome', 'completed') not in ('completed', 'failed'):
            invalid('Invalid fake executor settings')
        delay = settings.get('delay_seconds', .05)
        if type(delay) not in (int, float) or not 0 <= delay <= 60:
            invalid('Fake delay must be between zero and 60 seconds')

    def plan(self):
        return {'kind': 'in_process'}

    async def fence(self, handle):
        # Called only after its coroutine ended or the daemon acquired the
        # exclusive journal lock after the previous process exited.
        return True

    async def run(self, journal, attempt_id):
        async with journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
            if attempt.get('cancel'):
                return
            command = attempt['command']
            attempt['state'] = 'running'
            attempt['last_event_sequence'] = await journal.append(tx, 'attempt.started', {}, command)
            await tx.put('daemon_attempts', attempt_id, attempt)
        settings = command['payload']['agent'].get('settings', {})
        await asyncio.sleep(settings.get('delay_seconds', .05))
        async with journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
            attempt['last_event_sequence'] = await journal.append(tx, 'attempt.progress', {
                'kind': 'conversation', 'message_id': identity('msg'), 'role': 'agent', 'text': 'Fake executor processed the task.'}, command)
            await tx.put('daemon_attempts', attempt_id, attempt)
        await asyncio.sleep(settings.get('delay_seconds', .05))
        async with journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
            state = settings.get('outcome', 'completed')
            steering = list(attempt.get('steering', {}).values())
            payload = {'result': {'summary': ('Fake execution completed.' + (' Steering: ' + ' | '.join(steering) if steering else ''))[:4096]}} if state == 'completed' else {
                'error': {'code': 'PROVIDER_ERROR', 'message': 'Simulated executor failure', 'retryable': False}, 'reason': 'executor_failed'}
            await journal.finish(tx, attempt, state, payload)

