"""Cancellation cannot release owned resources or hide a worker failure."""
import asyncio

import pytest

from engine.ports.sandbox_tasks import settle_task


@pytest.mark.parametrize('fails', [False, True])
def test_owned_task_settles_before_repeated_cancellation_is_reported(fails):
    async def exercise():
        release = asyncio.Event()
        started = asyncio.Event()
        failure = RuntimeError('synthetic-worker-failure')
        async def work():
            await release.wait()
            if fails:
                raise failure
            return 'acquired-resource'
        worker = asyncio.create_task(work())
        async def owner():
            started.set()
            return await settle_task(worker)
        task = asyncio.create_task(owner())
        await started.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not worker.cancelled()
        release.set()
        if fails:
            with pytest.raises(RuntimeError) as error:
                await task
            assert error.value is failure
        else:
            resource, cancelled = await task
            assert resource == 'acquired-resource'
            assert cancelled
    asyncio.run(exercise())


def test_already_failed_worker_has_the_same_precedence_over_cancellation():
    async def exercise():
        failure = RuntimeError('synthetic-worker-failure')
        async def work():
            raise failure
        worker = asyncio.create_task(work())
        await asyncio.sleep(0)
        assert worker.done()
        async def owner():
            asyncio.current_task().cancel()
            await settle_task(worker)
        with pytest.raises(RuntimeError) as error:
            await asyncio.create_task(owner())
        assert error.value is failure
    asyncio.run(exercise())


def test_cancelled_worker_is_not_mistaken_for_a_pending_caller_cancellation():
    async def exercise():
        async def work():
            raise asyncio.CancelledError('worker-cancelled')
        with pytest.raises(asyncio.CancelledError, match='worker-cancelled'):
            await settle_task(asyncio.create_task(work()))
    asyncio.run(exercise())
