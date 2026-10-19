"""Own guest agent process groups independently of workspace policy."""
import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from engine.ports import SandboxInstance, SandboxProcess


_LAUNCH = '''import os, pathlib, sys, tempfile
if hasattr(os, 'setsid') and os.getpgrp() != os.getpid():
    os.setsid()
(pathlib.Path(tempfile.gettempdir()) / sys.argv[1]).write_text(str(os.getpid()))
os.execvp(sys.argv[2], sys.argv[2:])
'''
_PID = '''import pathlib, sys, tempfile, time
path = pathlib.Path(tempfile.gettempdir()) / sys.argv[1]
deadline = time.monotonic() + 10
while not path.exists():
    if time.monotonic() > deadline:
        raise TimeoutError('agent did not start its workspace process group')
    time.sleep(0.01)
print(path.read_text())
path.unlink()
'''
_STOP = '''import os, signal, sys
pid = int(sys.argv[1])
assert pid > 1 and pid != os.getpgrp()
try:
    os.killpg(pid, signal.SIGKILL)
except ProcessLookupError:
    pass
'''


async def guest_command(sandbox: SandboxInstance, script: str, *args: str, input: bytes | None = None) -> bytes:
    process = await sandbox.exec(("python3", "-c", script, *args))

    async def feed() -> None:
        try:
            if input is not None:
                for offset in range(0, len(input), 65536):
                    process.stdin.write(input[offset:offset + 65536])
                    await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Report the guest's exit status and stderr below, as communicate does.
            pass
        finally:
            process.stdin.close()

    stdout, stderr, code, _ = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait(), feed(),
    )
    if code:
        message = "sandbox guest command failed: " + (stderr or stdout).decode(errors="replace")[:2000]
        raise RuntimeError(message)
    return stdout


class AgentSandbox:
    """Own the agent's guest process group so export sees a stopped writer."""

    def __init__(self, sandbox: SandboxInstance) -> None:
        self.sandbox = sandbox
        self.processes: list[tuple[int, SandboxProcess]] = []

    async def exec(self, argv: Sequence[str], *, cwd: str = ".", env: Mapping[str, str] | None = None) -> SandboxProcess:
        control = ".oe-agent-" + uuid4().hex
        process = await self.sandbox.exec(("python3", "-c", _LAUNCH, control, *argv), cwd=cwd, env=env)
        pid = int(await guest_command(self.sandbox, _PID, control))
        self.processes.append((pid, process))
        return process

    async def stop(self) -> None:
        for pid, process in self.processes:
            # Closing ACP's stdin normally lets the agent flush its session
            # store. Bound that grace period, then stop remaining descendants.
            waiting = asyncio.create_task(process.wait())
            try:
                await asyncio.wait_for(asyncio.shield(waiting), timeout=2)
            except TimeoutError:
                pass
            finally:
                if not waiting.done():
                    waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await guest_command(self.sandbox, _STOP, str(pid))
            async with asyncio.timeout(10):
                await process.wait()

    @asynccontextmanager
    async def paused(self):
        # Freeze writers during live host-tool checkpoints. The broker and
        # transfer helpers run outside the agent's process groups.
        script = "import os,signal,sys\nfor value in sys.argv[2:]:\n try: os.killpg(int(value), getattr(signal,sys.argv[1]))\n except ProcessLookupError: pass"
        identifiers = tuple(str(pid) for pid, _ in self.processes)
        try:
            await guest_command(self.sandbox, script, "SIGSTOP", *identifiers)
            yield
        finally:
            await guest_command(self.sandbox, script, "SIGCONT", *identifiers)

    async def working_directory(self, path: str = ".") -> str:
        return await self.sandbox.working_directory(path)

    async def copy_in(self, source: Path, destination: str) -> None:
        await self.sandbox.copy_in(source, destination)

    async def copy_out(self, source: str, destination: Path) -> None:
        await self.sandbox.copy_out(source, destination)

    async def destroy(self) -> None:
        await self.sandbox.destroy()

