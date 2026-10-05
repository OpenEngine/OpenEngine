"""Host-specific guarantees and limitations, outside the reusable contract."""

import asyncio
from pathlib import Path
import sys

import pytest

from engine.adapters.sandbox.process import ProcessSandbox
from engine.ports import SandboxSpec


@pytest.mark.parametrize("spec", [SandboxSpec(image="linux"), SandboxSpec(egress_allowlist=())])
def test_unsupported_isolation_is_rejected(spec):
    async def run():
        with pytest.raises(ValueError, match="does not support"):
            async with ProcessSandbox().create(spec):
                pytest.fail("unsupported sandbox was created")
    asyncio.run(run())


def test_host_environment_and_workspace_cleanup(monkeypatch):
    monkeypatch.setenv("SANDBOX_INHERITED", "host")
    async def run():
        async with ProcessSandbox().create(SandboxSpec()) as sandbox:
            process = await sandbox.exec([sys.executable, "-c",
                "import os; print(os.getcwd()); print(os.environ['SANDBOX_INHERITED'])"])
            lines = (await process.stdout.read()).decode().splitlines()
            assert lines[1] == "host"
            root = Path(lines[0])
            assert root.is_dir()
            assert await process.wait() == 0
        assert not root.exists()
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        SandboxSpec(timeout=timeout)


def test_failed_workspace_copy_removes_temporary_directory(tmp_path, monkeypatch):
    import engine.adapters.sandbox.process as adapter

    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setattr(adapter.tempfile, "mkdtemp", lambda **kwargs: str(root))
    async def run():
        with pytest.raises(FileNotFoundError):
            async with ProcessSandbox().create(SandboxSpec(workspace=tmp_path / "missing")):
                pytest.fail("missing workspace was copied")
        assert not root.exists()
    asyncio.run(run())


def test_destroy_with_unread_output():
    async def run():
        async with ProcessSandbox().create(SandboxSpec()) as sandbox:
            process = await sandbox.exec([sys.executable, "-u", "-c",
                "import sys; print('ready'); "
                "exec('while True: sys.stdout.write(\"x\" * 65536)')"])
            assert await process.stdout.readline() == b"ready\n"
            await asyncio.sleep(0.1)
        assert await process.wait() != 0
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_timeout_kills_descendants_after_parent_exit():
    async def run():
        async with ProcessSandbox().create(SandboxSpec(timeout=0.2)) as sandbox:
            process = await sandbox.exec([sys.executable, "-c",
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"])
            output, code = await asyncio.gather(process.stdout.read(), process.wait())
            assert (output, code) == (b"", 0)
    asyncio.run(asyncio.wait_for(run(), 10))


def test_cancellation_during_spawn_is_cleaned_up(monkeypatch):
    import engine.adapters.sandbox.process as adapter

    original = asyncio.create_subprocess_exec

    async def run():
        spawned = asyncio.Event()
        release = asyncio.Event()
        child = None

        async def delayed_spawn(*args, **kwargs):
            nonlocal child
            child = await original(*args, **kwargs)
            spawned.set()
            await release.wait()
            return child

        monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", delayed_spawn)

        async def body():
            async with ProcessSandbox().create(SandboxSpec()) as sandbox:
                await sandbox.exec([sys.executable, "-c", "import time; time.sleep(60)"])

        task = asyncio.create_task(body())
        await spawned.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.returncode is not None

    asyncio.run(asyncio.wait_for(run(), 10))
