"""Binary ACP streams with explicit ownership, independent of the launcher."""

import asyncio
from typing import Protocol

from langgraph_acp.client import ACPClient
from langgraph_acp.elicitation import ACPElicitationHandler
from langgraph_acp.permissions import ACPPermissionHandler


class ACPTransport(Protocol):
    """A connection owns its transport; close must release its resources."""

    @property
    def stdin(self) -> asyncio.StreamWriter: ...
    @property
    def stdout(self) -> asyncio.StreamReader: ...
    @property
    def stderr(self) -> asyncio.StreamReader: ...
    @property
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...
    async def close(self) -> None: ...


async def connect_over_transport(
    *, agent: str, transport: ACPTransport,
    permissions: ACPPermissionHandler | None = None,
    elicitations: ACPElicitationHandler | None = None,
    session_cwd: str | None = None,
    allow_session_load: bool = True,
) -> ACPClient:
    """Initialize ACP over owned streams, releasing them on setup failure."""
    from langgraph_acp._stdio import StdioACPClient

    client = None
    try:
        client = StdioACPClient(
            agent=agent, transport=transport,
            permissions=permissions, elicitations=elicitations,
            session_cwd=session_cwd, allow_session_load=allow_session_load,
        )
        await client.initialize()
    except BaseException:
        if client is None:
            await transport.close()
        else:
            await client.close()
        raise
    else:
        return client


class SubprocessACPTransport:
    """Local process implementation; sandbox transports need no kill method."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self._process = process

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()

    async def close(self) -> None:
        self.stdin.close()
        for stop in (None, self._process.terminate, self._process.kill):
            if self.returncode is not None:
                return
            try:
                if stop is not None:
                    stop()
                await asyncio.wait_for(self.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                continue
            else:
                return


__all__ = ["ACPTransport", "connect_over_transport"]
