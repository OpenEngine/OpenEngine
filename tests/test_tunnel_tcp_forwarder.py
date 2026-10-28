"""A TCP forwarder in front of a tunnelled daemon must not let callers skip its token.

Setup: the daemon runs as `engine-web` does, on loopback, with `public_url` set
to a tunnel's address, a service token, and no GitHub login. In front of it
sits a plain TCP forwarder, as `ssh -R`, `socat` or a TCP-mode tunnel would be:
it copies bytes and adds no Forwarded / X-Forwarded-* / Cf-Connecting-Ip
header, so whatever the remote caller writes reaches the daemon unchanged.

Request: through the forwarder, `GET /api/v1/graphs` with `Host: 127.0.0.1`
and no Authorization header.

Expected: 401, because the caller is on the far side of the tunnel.
Actual: 200, because the daemon treats a loopback Host with no forwarding
headers as a local request, and both are the caller's to choose.
"""

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from engine.apps.web.__main__ import build_app

PUBLIC_HOST = "openengine.example.com"
TOKEN = "t" * 40


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The daemon on loopback with the tunnel's address as public_url, a token, and no GitHub login."""
    for name in (
        "ENGINE_HOST", "ENGINE_PORT", "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
        "ENGINE_GITHUB_LOGIN_CLIENT_ID", "ENGINE_GITHUB_LOGIN_REDIRECT_URI",
        "ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "ENGINE_SERVICE_TOKEN", "ENGINE_ONBOARDING_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("ENGINE_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "engine.toml"
    config.write_text(f'public_url = "https://{PUBLIC_HOST}"\n')
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(config), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "daemon did not start"
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def tcp_forwarder(daemon: int):
    """A byte-for-byte TCP forwarder to the daemon, like `ssh -R` or `socat`; it adds no headers."""
    ready = threading.Event()
    state: dict = {}

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def handle(client_reader, client_writer) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", daemon)
        await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer))

    async def serve() -> None:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        state["port"] = server.sockets[0].getsockname()[1]
        state["stop"] = asyncio.Event()
        state["loop"] = asyncio.get_running_loop()
        ready.set()
        async with server:
            await state["stop"].wait()

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(10)
    yield state["port"]
    state["loop"].call_soon_threadsafe(state["stop"].set)
    thread.join(timeout=10)


def test_a_tcp_forwarder_does_not_let_a_loopback_host_skip_the_token(tcp_forwarder: int) -> None:
    """A remote caller behind a header-less forwarder names a loopback Host and must still be refused."""
    response = httpx.get(
        f"http://127.0.0.1:{tcp_forwarder}/api/v1/graphs",
        headers={"Host": "127.0.0.1"},
        timeout=30,
    )
    assert response.status_code == 401, (
        f"through a TCP forwarder, Host: 127.0.0.1 with no token got {response.status_code}"
    )
