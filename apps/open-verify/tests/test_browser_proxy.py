import asyncio
from urllib.parse import urlsplit

import pytest
from network_fixtures import external_destination

from open_verify.artifacts import Artifacts
from open_verify.browser_proxy import BrowserProxy
from open_verify.tools import LocalTools


@pytest.mark.parametrize("allowed_scheme", [None, "http", "https"])
@pytest.mark.parametrize("protocol", ["tls", "websocket"])
def test_connect_checks_destination_and_closes_tunnel(tmp_path, allowed_scheme, protocol, monkeypatch):
    async def run():
        connections = []
        finished = asyncio.Event()

        async def destination(reader, writer):
            connections.append(True)
            try:
                while data := await reader.read(4096):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(destination, "127.0.0.1", 0)
        authority = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
        external_destination(monkeypatch, f"http://{authority}")
        tools = LocalTools(
            tmp_path,
            Artifacts(tmp_path / "runs"),
            allow_origins=[f"{allowed_scheme}://{authority}"] if allowed_scheme else [],
        )
        proxy = BrowserProxy(tools.check_url, tools.browser_events)
        writer = None
        try:
            address = urlsplit(await proxy.start())
            reader, writer = await asyncio.open_connection(address.hostname, address.port)
            writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            if allowed_scheme:
                assert response.startswith(b"HTTP/1.1 200")
                # CONNECT must forward bytes unchanged, with no TLS termination.
                payload = (
                    b"\x16\x03\x03opaque TLS bytes\r\n"
                    if protocol == "tls"
                    else f"GET /socket HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n".encode()
                )
                writer.write(payload)
                await writer.drain()
                actual_scheme = "https" if protocol == "tls" else "http"
                if allowed_scheme != actual_scheme:
                    assert await asyncio.wait_for(reader.read(), 3) == b""
                    assert connections == []
                    assert tools.browser_events == [
                        {"blocked_url": f"{actual_scheme}://{authority}/"}
                    ]
                    return
                assert await asyncio.wait_for(reader.readexactly(len(payload)), 3) == payload
                await asyncio.wait_for(proxy.close(), 3)
                assert await asyncio.wait_for(reader.read(), 3) == b""
                await asyncio.wait_for(finished.wait(), 3)
                assert connections == [True]
                assert not proxy.tasks
            else:
                assert response.startswith(b"HTTP/1.1 403")
                assert connections == []
                assert tools.browser_events == [{"blocked_url": f"https://{authority}/"}]
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            await proxy.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
