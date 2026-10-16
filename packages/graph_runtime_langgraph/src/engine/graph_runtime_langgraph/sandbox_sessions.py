"""Run-owned agent state, separate from workspaces and model credentials."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import shutil
import tempfile
from typing import Literal

from engine.ports.sandbox_sessions import SandboxSessions


def _key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AgentSessions:
    def __init__(self, directory: Path | None = None) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix='oe-agent-sessions-') if directory is None else None
        if directory is None:
            assert self._temporary is not None
            directory = Path(self._temporary.name)
        self.directory = directory.resolve()
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, run: str, key: str, provider: Literal['codex', 'claude']):
        identity = (run, key, provider)
        async with self._locks.setdefault(identity, asyncio.Lock()):
            directory = self.directory / _key(run) / _key(key) / provider
            for path in (self.directory, directory.parent.parent, directory.parent, directory):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.is_symlink():
                    message = 'agent session storage must not be a symlink'
                    raise ValueError(message)
                path.chmod(0o700)
            yield SandboxSessions(provider, directory)

    def delete(self, run: str) -> None:
        # The caller stops the run before deleting its metadata.
        target = self.directory / _key(run)
        if target.exists():
            shutil.rmtree(target)
        self._locks = {key: lock for key, lock in self._locks.items() if key[0] != run}
