"""Backend-neutral contract: add future factories to sandbox_backend's params."""

import asyncio
from pathlib import Path
import sys
import os

import pytest

from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.ports import Sandbox, SandboxInstance, SandboxProcess, SandboxSpec


@pytest.fixture(params=["process", "smolvm-fake", "smolvm"], ids=["process", "smolvm-fake", "smolvm"])
def sandbox_backend(request) -> Sandbox:
    if request.param == "process":
        return ProcessSandbox()
    if request.param == "smolvm-fake":
        launcher, _ = request.getfixturevalue("smolvm_cli")
        return SmolvmSandbox(image="fixture", executable=str(launcher))
    image = os.environ.get("OE_SMOLVM_TEST_IMAGE")
    if not image:
        pytest.skip("set OE_SMOLVM_TEST_IMAGE to run the contract on real SmolVM hardware")
    return SmolvmSandbox(image=image, executable=os.environ.get("OE_SMOLVM_EXECUTABLE", "smolvm"))


@pytest.fixture
def command(sandbox_backend):
    python = sys.executable if isinstance(sandbox_backend, ProcessSandbox) or sandbox_backend.image == "fixture" else "python3"
    return lambda script: [python, "-u", "-c", script]


def test_streaming_stdio_and_status(sandbox_backend, command):
    async def run():
        async with sandbox_backend.create(SandboxSpec()) as sandbox:
            assert isinstance(sandbox, SandboxInstance)
            process = await sandbox.exec(command(
                "import sys; print('ready'); "
                "print(sys.stdin.readline().strip()); "
                "print('error', file=sys.stderr); sys.exit(7)"
            ))
            assert isinstance(process, SandboxProcess)
            assert await process.stdout.readline() == b"ready\n"
            process.stdin.write(b"hello\n")
            await process.stdin.drain()
            process.stdin.close()
            out, err, code = await asyncio.gather(
                process.stdout.read(), process.stderr.read(), process.wait()
            )
            assert (out, err, code) == (b"hello\n", b"error\n", 7)
    asyncio.run(asyncio.wait_for(run(), 120))


def test_workspace_copy_and_transfers(sandbox_backend, tmp_path, command):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input").write_text("original")

    async def run():
        async with sandbox_backend.create(SandboxSpec(workspace=source)) as sandbox:
            await sandbox.copy_in(source, "nested")
            process = await sandbox.exec(command(
                "from pathlib import Path; "
                "assert Path('input').read_text() == 'original'; "
                "Path('input').write_text('changed')"
            ), cwd="nested")
            assert await process.wait() == 0
            await sandbox.copy_out("nested", tmp_path / "output")
            await sandbox.copy_out("input", tmp_path / "single")
        assert (tmp_path / "output/input").read_text() == "changed"
        assert (tmp_path / "single").read_text() == "original"
        assert (source / "input").read_text() == "original"
    asyncio.run(run())


def test_absolute_command_with_path_excluding_python(sandbox_backend):
    async def run():
        async with sandbox_backend.create(SandboxSpec()) as sandbox:
            process = await sandbox.exec(
                ["/bin/sh", "-c", 'printf "%s" "$PATH"'],
                env={"PATH": "/custom-tools"},
            )
            assert await process.stdout.read() == b"/custom-tools"
            assert await process.stderr.read() == b""
            assert await process.wait() == 0
    asyncio.run(asyncio.wait_for(run(), 120))


def test_environment_and_secrets(sandbox_backend, command):
    async def run():
        spec = SandboxSpec(secrets={"SANDBOX_TEST_SECRET": "private"})
        assert "private" not in repr(spec)
        async with sandbox_backend.create(spec) as sandbox:
            process = await sandbox.exec(command(
                "import os; print(os.environ['SANDBOX_TEST_SECRET']); "
                "print(os.environ['SANDBOX_TEST_ENV'])"
            ), env={"SANDBOX_TEST_ENV": "value"})
            assert await process.stdout.read() == b"private\nvalue\n"
            assert await process.wait() == 0
    asyncio.run(run())


@pytest.mark.parametrize("exit_kind", ["normal", "exception", "cancel"])
def test_context_always_destroys(sandbox_backend, exit_kind, command):
    async def run():
        sandbox = None
        process = None
        async def body():
            nonlocal sandbox, process
            async with sandbox_backend.create(SandboxSpec()) as sandbox:
                process = await sandbox.exec(command("import time; print('ready'); time.sleep(60)"))
                assert await process.stdout.readline() == b"ready\n"
                if exit_kind == "exception":
                    raise ValueError("body failed")
                if exit_kind == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
        task = asyncio.create_task(body())
        if exit_kind == "exception":
            with pytest.raises(ValueError, match="body failed"):
                await task
        elif exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert await process.wait() != 0
        await sandbox.destroy()
        with pytest.raises(RuntimeError, match="destroyed"):
            await sandbox.exec(command("pass"))
    asyncio.run(asyncio.wait_for(run(), 120))


def test_timeout_starts_at_exec(sandbox_backend, command):
    async def run():
        async with sandbox_backend.create(SandboxSpec(timeout=0.1)) as sandbox:
            process = await sandbox.exec(command("import time; time.sleep(60)"))
            # EOF must arrive without wait() triggering the timeout.
            assert await process.stdout.read() == b""
            with pytest.raises(TimeoutError):
                await process.wait()
    asyncio.run(asyncio.wait_for(run(), 120))


def test_path_escape_rejected(sandbox_backend, tmp_path, command):
    async def run():
        async with sandbox_backend.create(SandboxSpec()) as sandbox:
            for path in ("../escape", str(tmp_path)):
                with pytest.raises(ValueError):
                    await sandbox.exec(command("pass"), cwd=path)
                with pytest.raises(ValueError):
                    await sandbox.copy_in(tmp_path, path)
                with pytest.raises(ValueError):
                    await sandbox.copy_out(path, tmp_path / "output")
    asyncio.run(run())


def test_failed_exec_does_not_break_context(sandbox_backend, command):
    async def run():
        async with sandbox_backend.create(SandboxSpec()) as sandbox:
            with pytest.raises(FileNotFoundError):
                await sandbox.exec(["/nonexistent/engine-sandbox-command"])
            process = await sandbox.exec(command("pass"))
            assert await process.wait() == 0
    asyncio.run(run())
