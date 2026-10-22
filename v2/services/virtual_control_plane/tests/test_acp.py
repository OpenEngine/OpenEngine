import asyncio
from types import SimpleNamespace

import pytest
from virtual_control_plane.daemon.acp import ACPClient


def test_request_timeout_includes_stalled_stdin_drain():
    class StalledWriter:
        def write(self, data):
            self.data = data

        async def drain(self):
            await asyncio.Event().wait()

    class Reader:
        async def read(self, size):
            return b''

        async def readline(self):
            pytest.fail('Request must time out during drain, before reading a response')

    async def scenario():
        writer = StalledWriter()
        client = ACPClient(SimpleNamespace(stdin=writer, stdout=Reader(), stderr=Reader()))
        task = asyncio.create_task(client.call('session/prompt', {'prompt': 'x' * 200000}, timeout=.05))
        try:
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, 'Configured request timeout did not interrupt drain'
            with pytest.raises(TimeoutError):
                await task
            assert len(writer.data) > 200000
        finally:
            task.cancel()
            await asyncio.gather(task, client.stderr, return_exceptions=True)
    asyncio.run(scenario())


def test_close_drains_unread_output_after_fencing():
    class Process:
        def __init__(self):
            self.stdout_drained = asyncio.Event()
            self.stdout = self
            self.stderr = self
            self.stdin = self

        def close(self):
            pass

        async def read(self, size):
            self.stdout_drained.set()
            return b''

        async def wait(self):
            await self.stdout_drained.wait()

    async def scenario():
        process = Process()
        client = ACPClient(process)
        # Ensure the proof is about stdout, not the independently drained stderr.
        await client.stderr
        process.stdout_drained.clear()
        await asyncio.wait_for(client.close(), timeout=1)
        assert process.stdout_drained.is_set()
    asyncio.run(scenario())
