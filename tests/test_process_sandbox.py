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


@pytest.mark.parametrize("cancellations", [1, 2, 3])
def test_cancellation_during_spawn_is_cleaned_up(monkeypatch, cancellations):
    import engine.adapters.sandbox.process as adapter

    original = asyncio.create_subprocess_exec

    async def run():
        spawned = asyncio.Event()
        release = asyncio.Event()
        child = None
        root = None

        async def delayed_spawn(*args, **kwargs):
            nonlocal child
            child = await original(*args, **kwargs)
            spawned.set()
            await release.wait()
            return child

        monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", delayed_spawn)

        async def body():
            nonlocal root
            async with ProcessSandbox().create(SandboxSpec()) as sandbox:
                root = sandbox._root
                await sandbox.exec([sys.executable, "-c", "import time; time.sleep(60)"])

        task = asyncio.create_task(body())
        try:
            await spawned.wait()
            for _ in range(cancellations):
                task.cancel()
                # Deliver each cancellation while spawn recovery is pending.
                await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert child.returncode is not None
            assert not root.exists()
        finally:
            # A failing regression must not leave its real subprocess behind.
            release.set()
            if child is not None and child.returncode is None:
                adapter._kill(child)
                await child.communicate()

    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("operation", ["create", "copy_in", "copy_out"])
def test_nested_source_links_are_preserved(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    (host / "secret").write_text("host data")
    (source / "nested").mkdir()
    (source / "nested/link").symlink_to(host, target_is_directory=True)
    (source / "nested/dangling").symlink_to(host / "missing")

    async def run():
        spec = SandboxSpec(workspace=source) if operation == "create" else SandboxSpec()
        async with ProcessSandbox().create(spec) as sandbox:
            if operation == "copy_in":
                await sandbox.copy_in(source, "copied")
                result = sandbox._root / "copied"
            elif operation == "copy_out":
                (sandbox._root / "nested").mkdir()
                (sandbox._root / "nested/link").symlink_to(host, target_is_directory=True)
                (sandbox._root / "nested/dangling").symlink_to(host / "missing")
                result = tmp_path / "output"
                await sandbox.copy_out(".", result)
            else:
                result = sandbox._root
            assert (result / "nested/link").is_symlink()
            assert (result / "nested/link").readlink() == host
            assert (result / "nested/dangling").is_symlink()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["copy_in", "copy_out"])
@pytest.mark.parametrize("kind", ["directory", "file", "dangling"])
def test_nested_destination_links_are_rejected(tmp_path, operation, kind):
    host = tmp_path / "host"
    host.mkdir()
    (host / "file").write_text("original")
    source = tmp_path / "source"
    source.mkdir()
    if kind == "directory":
        (source / "sub").mkdir()
        (source / "sub/file").write_text("replacement")
        target = host
    else:
        (source / "sub").write_text("replacement")
        target = host / ("file" if kind == "file" else "missing")

    async def run():
        async with ProcessSandbox().create(SandboxSpec(workspace=source)) as sandbox:
            destination = sandbox._root / "dst" if operation == "copy_in" else tmp_path / "dst"
            destination.mkdir()
            (destination / "sub").symlink_to(target, target_is_directory=kind == "directory")
            with pytest.raises(ValueError, match="symlink"):
                if operation == "copy_in":
                    await sandbox.copy_in(source, "dst")
                else:
                    await sandbox.copy_out(".", destination)
            assert (host / "file").read_text() == "original"
            assert not (host / "missing").exists()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["create", "copy_in", "copy_out", "destroy"])
def test_filesystem_work_does_not_block_and_survives_cancellation(tmp_path, monkeypatch, operation):
    import threading
    import engine.adapters.sandbox.process as adapter

    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("data")
    release = threading.Event()

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        root = None
        original = adapter.shutil.rmtree if operation == "destroy" else adapter._copy

        def blocked(*args):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "event loop did not release filesystem worker"
            assert root is None or root.exists()
            return original(*args)

        async def body():
            nonlocal root
            spec = SandboxSpec(workspace=source) if operation == "create" else SandboxSpec()
            async with ProcessSandbox().create(spec) as sandbox:
                root = sandbox._root
                if operation == "copy_in":
                    await sandbox.copy_in(source, "dst")
                elif operation == "copy_out":
                    await sandbox.copy_out(".", tmp_path / "out")

        if operation == "destroy":
            monkeypatch.setattr(adapter.shutil, "rmtree", blocked)
        else:
            monkeypatch.setattr(adapter, "_copy", blocked)
        task = asyncio.create_task(body())
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            if root is not None:
                assert root.exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if root is not None:
            assert not root.exists()
    asyncio.run(asyncio.wait_for(run(), 10))


def test_completed_executions_are_retired():
    async def run():
        async with ProcessSandbox().create(SandboxSpec(timeout=60)) as sandbox:
            for _ in range(20):
                process = await sandbox.exec([sys.executable, "-c", "print('done')"])
                assert await process.wait() == 0
                assert await process.stdout.read() == b"done\n"
                assert process._timer.cancelled()
                assert not sandbox._executions
    asyncio.run(asyncio.wait_for(run(), 10))


def test_destroy_waits_for_cancelled_copy(tmp_path, monkeypatch):
    import threading
    import engine.adapters.sandbox.process as adapter

    source = tmp_path / "input"
    source.write_text("data")
    release = threading.Event()
    original = adapter._copy

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        async with ProcessSandbox().create(SandboxSpec()) as sandbox:
            def blocked(*args):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                assert sandbox._root.exists()
                original(*args)

            monkeypatch.setattr(adapter, "_copy", blocked)
            copying = asyncio.create_task(sandbox.copy_in(source, "input"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                copying.cancel()
                await asyncio.sleep(0)
                copying.cancel()
                destroying = asyncio.create_task(sandbox.destroy())
                await asyncio.sleep(0)
                assert not copying.done()
                assert not destroying.done()
                assert sandbox._root.exists()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await copying
            await destroying
            assert not sandbox._root.exists()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_retirement_stops_descendants_with_closed_pipes(tmp_path):
    async def run():
        async with ProcessSandbox().create(SandboxSpec()) as sandbox:
            marker = tmp_path / "survived"
            script = f"import time; from pathlib import Path; time.sleep(0.3); Path({str(marker)!r}).touch()"
            process = await sandbox.exec([sys.executable, "-c",
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {script!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"])
            assert await process.wait() == 0
            assert not sandbox._executions
            await asyncio.sleep(0.5)
            assert not marker.exists()
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
@pytest.mark.parametrize("readonly", [".", "nested", "both"])
def test_readonly_workspace_teardown(tmp_path, readonly):
    import stat

    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "file").write_text("root")
    (nested / "file").write_text("nested")
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_text("untouched")
    external.chmod(0o555)
    (nested / "link").symlink_to(external, target_is_directory=True)
    directories = [source, nested] if readonly == "both" else [source / readonly]
    for directory in directories:
        directory.chmod(0o555)

    async def run():
        async with ProcessSandbox().create(SandboxSpec(workspace=source)) as sandbox:
            root = sandbox._root
            for directory in directories:
                assert stat.S_IMODE((root / directory.relative_to(source)).stat().st_mode) == 0o555
        assert not root.exists()
        await sandbox.destroy()
        for directory in directories:
            assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        assert stat.S_IMODE(external.stat().st_mode) == 0o555
        assert (external / "file").read_text() == "untouched"

    try:
        asyncio.run(run())
    finally:
        source.chmod(0o755)
        nested.chmod(0o755)
        external.chmod(0o755)


def test_destroy_survives_repeated_cancellation_during_copy(tmp_path, monkeypatch):
    import threading
    import engine.adapters.sandbox.process as adapter

    source = tmp_path / "input"
    source.write_text("data")
    release = threading.Event()
    original = adapter._copy

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        async with ProcessSandbox().create(SandboxSpec()) as sandbox:
            process = await sandbox.exec([sys.executable, "-u", "-c",
                "import time; print('ready'); time.sleep(60)"])
            assert await process.stdout.readline() == b"ready\n"

            def blocked(*args):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                original(*args)

            monkeypatch.setattr(adapter, "_copy", blocked)
            copying = asyncio.create_task(sandbox.copy_in(source, "input"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                destroying = asyncio.create_task(sandbox.destroy())
                await asyncio.sleep(0)
                for _ in range(3):
                    destroying.cancel()
                    await asyncio.sleep(0)
                assert not destroying.done()
                assert not sandbox._cleanup.cancelled()
                assert sandbox._root.exists()
            finally:
                release.set()
            await copying
            with pytest.raises(asyncio.CancelledError):
                await destroying
            assert not sandbox._root.exists()
            assert await process.wait() != 0
            await sandbox.destroy()

    asyncio.run(asyncio.wait_for(run(), 10))
