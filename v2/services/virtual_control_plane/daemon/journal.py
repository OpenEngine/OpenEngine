"""Durable daemon inbox and ordered updates, with executor-specific recovery fencing."""
from virtual_control_plane.domain.runtime import invalid
from virtual_control_plane.daemon.fake import FakeBackend
from pathlib import Path

from virtual_control_plane.domain.lifecycle import identity, now
from virtual_control_plane.transport.nats.runtime_protocol import Protocol
from virtual_control_plane.domain.runtime import digest, error

TERMINAL = ('completed', 'failed', 'cancelled')


class Journal:
    def __init__(self, storage, target, workspace_root, *, max_pending=1000, backends=None):
        self.storage, self.target = storage, target
        self.root = Path(workspace_root).resolve(strict=True)
        self.boot_id = identity('boot')
        self.protocol = Protocol()
        self.max_pending = max_pending
        self.backends = backends if backends is not None else {'fake': FakeBackend()}

    async def append(self, tx, kind, payload, command=None, termination=None, cancel=None, steer=None):
        meta = await tx.get('daemon_meta', 'runtime')
        meta['sequence'] += 1
        message = {'event_id': identity('rte'), 'kind': kind, 'target': command['target'] if command else self.target,
                   'boot_id': self.boot_id, 'sequence': meta['sequence'], 'occurred_at': now(), 'payload': payload}
        if command:
            message['execution'] = command['execution']
        if steer:
            message.update(steer_command_id=steer['command_id'], cause=steer['cause'])
        if cancel:
            message.update(cancel_command_id=cancel['command_id'], cause=cancel['cause'])
        if termination:
            message['termination'] = termination
        wire = self.protocol.encode('update', message).decode('utf-8')
        await tx.put('daemon_updates', str(message['sequence']), {'message': message, 'wire': wire, 'acknowledged': False})
        await tx.put('daemon_meta', 'runtime', meta)
        return message['sequence']

    async def recover(self):
        async with self.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            if meta is None:
                meta = {'target': self.target, 'journal_id': identity('journal'), 'sequence': 0}
                await tx.put('daemon_meta', 'runtime', meta)
            if meta['target'] != self.target:
                invalid('Journal binding differs from provisioned runtime')
            meta.update(ready=False)
            await tx.put('daemon_meta', 'runtime', meta)
            attempts = await tx.all('daemon_attempts')
            for attempt in attempts:
                if attempt['state'] == 'recorded' and attempt['command']['payload']['agent']['runner'] not in self.backends:
                    await self.finish(tx, attempt, 'failed', {'error': {
                        'code': 'PROVIDER_ERROR', 'message': 'Executor is no longer configured',
                        'retryable': False}, 'reason': 'launch_failed'}, 'never_started')
            interrupted = [attempt for attempt in attempts if attempt['state'] in ('launching', 'running', 'cancelling')]
        # Fencing may wait for external processes. Never hold a DB transaction
        # across it, and never treat a missing backend as proof of termination.
        for attempt in interrupted:
            backend = self.backends.get(attempt['command']['payload']['agent']['runner'])
            if not backend or not await backend.fence(attempt.get('executor_handle', {})):
                invalid('Cannot fence interrupted executor; runtime remains unready')
            async with self.storage.transaction() as tx:
                attempt = await tx.get('daemon_attempts', attempt['command']['target']['attempt_id'])
                await self.finish(tx, attempt, 'failed', {'error': {
                    'code': 'PROVIDER_ERROR', 'message': 'Executor was fenced after daemon restart',
                    'retryable': False}, 'reason': 'executor_lost'}, 'killed')
        async with self.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            sequence = await self.append(tx, 'runtime.ready', {'executors': sorted(self.backends), 'progress_kinds': sorted({kind for backend in self.backends.values() for kind in backend.progress_kinds}),
                                         'max_concurrent_attempts': 4, 'journal_id': meta['journal_id'],
                                         'steering_executors': sorted(name for name, backend in self.backends.items() if getattr(backend, 'supports_steering', False))})
            meta = await tx.get('daemon_meta', 'runtime')
            meta.update(ready_sequence=sequence, ready=False, blocked=False)
            await tx.put('daemon_meta', 'runtime', meta)

    def mismatch(self, subject, command, action):
        if subject != f"oe.internal.v1.runtime.runtimes.{self.target['runtime_id']}.{action}" or any(
                command['target'][key] != self.target[key] for key in ('workspace_id', 'runtime_id')):
            return error('IDENTITY_MISMATCH', 'Wrong runtime identity')
        if command['target']['runtime_generation'] != self.target['runtime_generation']:
            return error('STALE_RUNTIME', 'Wrong runtime generation')
        return None

    def validate_context(self, command):
        payload = command['payload']
        context = payload['context']
        if context['references']:
            invalid('Artifact reference resolution is not configured')
        for name in [context['working_directory'], *context['instruction_files']]:
            if Path(name).is_absolute() or '..' in name.split('/') or '\\' in name or '\x00' in name:
                invalid('Context path escapes workspace')
            resolved = (self.root / name).resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                invalid('Context symlink escapes workspace')
        if not (self.root / context['working_directory']).is_dir():
            invalid('Working directory does not exist')
        if any(not (self.root / path).is_file() for path in context['instruction_files']):
            invalid('Instruction file does not exist')
        self.backends[payload['agent']['runner']].validate(payload)

    async def accept(self, subject, command):
        if command['kind'] == 'steer':
            return await self.steer(subject, command)
        if command['kind'] == 'cancel':
            return await self.cancel(subject, command)
        response = {'command_id': command['command_id'], 'target': command['target'], 'status': 'accepted'}
        mismatch = self.mismatch(subject, command, 'execute')
        if mismatch:
            return {**response, 'status': 'rejected', 'error': mismatch}
        fingerprint = digest([subject, command])
        async with self.storage.transaction() as tx:
            previous = await tx.get('daemon_commands', command['command_id'])
            if previous:
                if previous['digest'] == fingerprint:
                    return previous['response']
                return {**response, 'status': 'rejected', 'error': error('COMMAND_ID_CONFLICT', 'Command identity changed')}
            meta = await tx.get('daemon_meta', 'runtime')
            attempt = await tx.get('daemon_attempts', command['target']['attempt_id'])
            session_id = command['target']['session_id']
            session = await tx.get('daemon_sessions', session_id)
            detail = None
            if attempt and attempt.get('cancel') and attempt['command']['target'] == command['target'] and attempt['command']['execution'] == command['execution']:
                # A matching execute retry after the tombstone is acknowledged,
                # but it can never enqueue a launch or replace the terminal event.
                await tx.put('daemon_commands', command['command_id'], {'digest': fingerprint, 'response': response})
                return response
            if attempt:
                detail = error('ATTEMPT_CONFLICT', 'Attempt already owned by another execute')
            elif command['payload']['agent']['runner'] not in self.backends:
                detail = error('UNSUPPORTED_EXECUTOR', 'Executor is not configured')
            elif session and session['active_attempt']:
                detail = error('SESSION_BUSY', 'Session has an active attempt')
            elif (command['payload']['session_mode'] == 'continue' and not session) or (command['payload']['session_mode'] == 'new' and session):
                detail = error('INVALID_ARGUMENT', 'Session conversation mapping does not match session mode')
            elif session and session['agent'] != command['payload']['agent']:
                detail = error('INVALID_ARGUMENT', 'Session agent configuration changed')
            if detail is None:
                try:
                    self.validate_context(command)
                except (ValueError, OSError):
                    detail = error('INVALID_ARGUMENT', 'Invalid or unavailable executor context')
            if detail is None:
                pending = sum(not row['acknowledged'] for row in await tx.all('daemon_updates'))
                active = sum(row['state'] not in TERMINAL for row in await tx.all('daemon_attempts'))
                if not meta['ready'] or meta.get('blocked') or pending + active * 3 >= self.max_pending or active >= 4:
                    return {**response, 'status': 'rejected', 'error': error('UNAVAILABLE', 'Runtime recovering or at capacity')}
            if detail:
                response.update(status='rejected', error=detail)
            else:
                await tx.put('daemon_attempts', command['target']['attempt_id'], {'command': command, 'state': 'recorded',
                             'last_event_sequence': 0, 'launch_count': 0})
                await tx.put('daemon_sessions', session_id, {**(session or {}), 'agent': command['payload']['agent'],
                             'active_attempt': command['target']['attempt_id']})
            await tx.put('daemon_commands', command['command_id'], {'digest': fingerprint, 'response': response})
        return response

    async def steer(self, subject, command):
        response = {'command_id': command['command_id'], 'target': command['target'], 'status': 'accepted'}
        mismatch = self.mismatch(subject, command, 'steer')
        if mismatch:
            return {**response, 'status': 'rejected', 'error': mismatch}
        fingerprint = digest([subject, command])
        async with self.storage.transaction() as tx:
            previous = await tx.get('daemon_commands', command['command_id'])
            if previous:
                if previous['digest'] == fingerprint:
                    return previous['response']
                return {**response, 'status': 'rejected', 'error': error('COMMAND_ID_CONFLICT', 'Command identity changed')}
            meta = await tx.get('daemon_meta', 'runtime')
            pending = sum(row.get('state') == 'steer_pending' for row in await tx.all('daemon_commands'))
            updates = sum(not row['acknowledged'] for row in await tx.all('daemon_updates'))
            if not meta['ready'] or meta.get('blocked') or pending + updates >= self.max_pending:
                return {**response, 'status': 'rejected', 'error': error('UNAVAILABLE', 'Runtime recovering or at capacity')}
            # State/capability checks happen at application, not acknowledgment.
            # They may change after this commit and must produce an outcome.
            await tx.put('daemon_commands', command['command_id'], {'digest': fingerprint, 'response': response,
                         'command': command, 'state': 'steer_pending', 'accepted_at': now()})
        return response

    async def cancel(self, subject, command):
        response = {'command_id': command['command_id'], 'target': command['target'], 'status': 'accepted'}
        mismatch = self.mismatch(subject, command, 'cancel')
        if mismatch:
            return {**response, 'status': 'rejected', 'error': mismatch}
        fingerprint = digest([subject, command])
        async with self.storage.transaction() as tx:
            previous = await tx.get('daemon_commands', command['command_id'])
            if previous:
                if previous['digest'] == fingerprint:
                    return previous['response']
                return {**response, 'status': 'rejected', 'error': error('COMMAND_ID_CONFLICT', 'Command identity changed')}
            execution_receipt = await tx.get('daemon_commands', command['execution']['command_id'])
            if execution_receipt and execution_receipt['response']['target'] != command['target']:
                return {**response, 'status': 'rejected', 'error': error('ATTEMPT_CONFLICT', 'Execute target differs')}
            if execution_receipt and execution_receipt['response']['status'] == 'rejected':
                # VCP projects a durable execute admission rejection itself.
                # No launch exists, and a second terminal update would conflict.
                await tx.put('daemon_commands', command['command_id'], {'digest': fingerprint, 'response': response})
                return response
            attempt_id = command['target']['attempt_id']
            attempt = await tx.get('daemon_attempts', attempt_id)
            if attempt and (attempt['command']['target'] != command['target'] or attempt['command']['execution'] != command['execution']):
                response.update(status='rejected', error=error('ATTEMPT_CONFLICT', 'Cancellation execution binding differs'))
            elif not attempt or attempt['state'] not in TERMINAL:
                if not attempt:
                    # This minimal command retains the exact execute identity;
                    # no runner is needed because launch is permanently forbidden.
                    attempt = {'command': command, 'state': 'recorded', 'last_event_sequence': 0, 'launch_count': 0}
                attempt.setdefault('cancel', command)
                if attempt['state'] == 'recorded':
                    await self.finish(tx, attempt, 'cancelled', {}, 'never_started')
                else:
                    attempt['state'] = 'cancelling'
                    await tx.put('daemon_attempts', attempt_id, attempt)
            await tx.put('daemon_commands', command['command_id'], {'digest': fingerprint, 'response': response})
        return response

    async def finish(self, tx, attempt, state, payload, termination='exited'):
        if attempt['state'] in TERMINAL:
            return
        command = attempt['command']
        cancel = attempt.get('cancel')
        if cancel:
            state, payload = 'cancelled', cancel['payload']
        attempt['state'] = state
        attempt['last_event_sequence'] = await self.append(tx, 'attempt.' + state, payload, command, termination, cancel)
        await tx.put('daemon_attempts', command['target']['attempt_id'], attempt)
        session_id = command['target']['session_id']
        session = await tx.get('daemon_sessions', session_id)
        if session and session.get('active_attempt') == command['target']['attempt_id']:
            session['active_attempt'] = None
            await tx.put('daemon_sessions', session_id, session)

    async def inspect(self, subject, request):
        response = {'request_id': request['request_id'], 'target': request['target'], 'status': 'ok'}
        mismatch = self.mismatch(subject, request, 'inspect')
        if mismatch:
            return {**response, 'status': 'error', 'error': mismatch}
        async with self.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            attempts = []
            for attempt_id in request['attempt_ids']:
                item = await tx.get('daemon_attempts', attempt_id)
                snapshot = {'attempt_id': attempt_id, 'known': item is not None}
                if item:
                    snapshot.update(target=item['command']['target'], execution=item['command']['execution'],
                                    state=item['state'], last_event_sequence=item['last_event_sequence'])
                attempts.append(snapshot)
            response['result'] = {'boot_id': self.boot_id, 'ready': meta['ready'] and not meta.get('blocked'),
                                  'last_event_sequence': meta['sequence'], 'attempts': attempts}
        return response

    async def progress(self, attempt_id, payload):
        import asyncio
        while True:
            async with self.storage.transaction() as tx:
                attempt = await tx.get('daemon_attempts', attempt_id)
                if attempt['state'] in TERMINAL:
                    return
                pending = sum(not row['acknowledged'] for row in await tx.all('daemon_updates'))
                active = sum(row['state'] not in TERMINAL for row in await tx.all('daemon_attempts'))
                if pending + active * 2 < self.max_pending:
                    attempt = await tx.get('daemon_attempts', attempt_id)
                    attempt['last_event_sequence'] = await self.append(tx, 'attempt.progress', payload, attempt['command'])
                    await tx.put('daemon_attempts', attempt_id, attempt)
                    return
            await asyncio.sleep(.1)
