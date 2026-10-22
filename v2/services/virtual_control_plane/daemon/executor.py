"""Supervise executor tasks and reconcile only after backend termination confirmation."""
import asyncio
import logging

from virtual_control_plane.domain.runtime import invalid
from virtual_control_plane.daemon.journal import TERMINAL

logger = logging.getLogger(__name__)


class ExecutionWorker:
    def __init__(self, journal, *, cancel_grace=2):
        self.journal = journal
        # Keep completed tasks until their durable outcome is reconciled.
        # A failed reconciliation write leaves the task here for the next poll.
        self.tasks = {}
        self.cancellations = {}
        self.cancel_grace = cancel_grace

    async def reap(self):
        recovered = False
        for task, attempt_id in list(self.tasks.items()):
            cancellation = self.cancellations.get(attempt_id)
            if cancellation and not cancellation.done():
                continue
            if cancellation:
                failure = None if cancellation.cancelled() else cancellation.exception()
                if failure:
                    logger.error('Graceful cancellation failed; fencing executor',
                                 exc_info=(type(failure), failure, failure.__traceback__))
                self.cancellations.pop(attempt_id)
            if not task.done():
                continue
            exception = None if task.cancelled() else task.exception()
            if exception:
                logger.error('Executor stopped; reconciling durable outcome',
                             exc_info=(type(exception), exception, exception.__traceback__))
            async with self.journal.storage.transaction() as tx:
                attempt = await tx.get('daemon_attempts', attempt_id)
            backend = self.journal.backends.get(attempt['command']['payload']['agent']['runner'])
            if not backend or not await backend.fence(attempt.get('executor_handle', {})):
                invalid('Executor termination is not confirmed; retaining ownership')
            async with self.journal.storage.transaction() as tx:
                attempt = await tx.get('daemon_attempts', attempt_id)
                if attempt['state'] not in TERMINAL:
                    # The backend has fenced every process before releasing
                    # durable ownership. Never repeat an uncertain launch.
                    await self.journal.finish(tx, attempt, 'failed', {'error': {
                        'code': 'PROVIDER_ERROR', 'message': 'Executor stopped before durable outcome',
                        'retryable': False}, 'reason': 'executor_lost'}, 'exited')
                    recovered = True
            self.tasks.pop(task)
        return recovered

    async def stop_attempt(self, task, backend, attempt_id):
        # The whole graceful phase, including pipe writes, has one deadline.
        try:
            async with asyncio.timeout(self.cancel_grace):
                cancel = getattr(backend, 'cancel', None)
                if cancel:
                    await cancel(attempt_id)
                    await asyncio.wait({task})
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            if not task.done():
                task.cancel()
            # Backend cleanup fences its tree. Reap independently retries a
            # failed fence or journal write before committing a terminal update.
            await asyncio.gather(task, return_exceptions=True)

    async def cancel_pending(self):
        async with self.journal.storage.transaction() as tx:
            pending = {row['command']['target']['attempt_id']: row for row in await tx.all('daemon_attempts')
                       if row['state'] == 'cancelling'}
        for task, attempt_id in self.tasks.items():
            if attempt_id in pending and attempt_id not in self.cancellations and not task.done():
                backend = self.journal.backends[pending[attempt_id]['command']['payload']['agent']['runner']]
                self.cancellations[attempt_id] = asyncio.create_task(self.stop_attempt(task, backend, attempt_id))

    async def run_once(self):
        await self.cancel_pending()
        recovered = await self.reap()
        async with self.journal.storage.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            if not meta['ready'] or meta.get('blocked'):
                return recovered
            rows = [row for row in await tx.all('daemon_attempts') if row['state'] == 'recorded']
            if not rows:
                return recovered
            attempt = rows[0]
            backend = self.journal.backends.get(attempt['command']['payload']['agent']['runner'])
            if backend is None:
                return recovered
            attempt['executor_handle'] = backend.plan()
            attempt['state'] = 'launching'
            attempt['launch_count'] += 1
            await tx.put('daemon_attempts', attempt['command']['target']['attempt_id'], attempt)
        # Intent is committed before scheduling any executor effect.
        task = asyncio.create_task(self.run(attempt['command']['target']['attempt_id']))
        self.tasks[task] = attempt['command']['target']['attempt_id']
        return True

    async def run(self, attempt_id):
        async with self.journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
        await self.journal.backends[attempt['command']['payload']['agent']['runner']].run(self.journal, attempt_id)

    async def close(self):
        for cancellation in self.cancellations.values():
            cancellation.cancel()
        await asyncio.gather(*self.cancellations.values(), return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


# Compatibility name for existing fake-runtime fixtures.
FakeExecutor = ExecutionWorker
