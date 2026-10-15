"""Security of the url-agent endpoint poller (#813), driven through the daemon's own API.

Each test adds an agent the way a client of the daemon does (POST /agents) and lets the
poller probe it. The endpoints are real loopback servers wherever the finding is about
what goes over the wire, so httpx's real transport and timeouts are what is under test.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from engine.graph_service import GraphService, create_app

from test_graph_service import graph_service

Handler = Callable[[str, dict[str, str], asyncio.StreamWriter], Awaitable[None]]


class LoopbackServer:
    """A bare HTTP/1.1 server on 127.0.0.1 that keeps every request it reads."""

    def __init__(self, handle: Handler) -> None:
        self.handle = handle
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.port = 0

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            line, *fields = head.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, _, v in (f.partition(":") for f in fields if f)}
            target = line.split(" ")[1]
            self.requests.append((target, headers))
            await self.handle(target, headers, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @asynccontextmanager
    async def running(self) -> AsyncIterator["LoopbackServer"]:
        server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        try:
            yield self
        finally:
            server.close()


async def answer_json(writer: asyncio.StreamWriter, body: bytes) -> None:
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()


async def ollama_running_qwen(target: str, headers: dict[str, str], writer: asyncio.StreamWriter) -> None:
    await answer_json(writer, b'{"models": [{"name": "qwen3-coder:latest", "model": "qwen3-coder:latest"}]}')


async def add_agent(service: GraphService, name: str, url: str) -> None:
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        added = await client.post("/agents", json={"kind": "opencode", "name": name, "model": "qwen3-coder", "url": url})
        assert added.status_code == 201, added.text


async def served_health(service: GraphService, name: str) -> Any:
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        agents = (await client.get("/agents")).json()["agents"]
    return next(agent for agent in agents if agent["name"] == name)["health"]


def test_the_poller_does_not_probe_the_cloud_metadata_address_for_a_client(tmp_path: Path) -> None:
    """A client that can add an agent must not get the daemon to probe 169.254.169.254 and report back.

    The poller requests the url on its own every pass and serves whether it answered, so
    without a restriction any client of the daemon gets a reachability oracle for the
    instance metadata service (and any other internal address) from the daemon's network.
    """
    probed: list[str] = []

    def metadata_service(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        return httpx.Response(200, json={"models": []})

    async def scenario() -> None:
        async with graph_service(tmp_path, endpoint_transport=httpx.MockTransport(metadata_service)) as service:
            await add_agent(service, "imds", "http://169.254.169.254/latest")
            await service.poll_endpoints()
            assert probed == [], f"the poller requested the metadata service on its own: {probed}"
            assert await served_health(service, "imds") is None

    asyncio.run(scenario())


def test_a_probe_stops_reading_an_endless_body_instead_of_buffering_it(tmp_path: Path) -> None:
    """A model endpoint that streams an endless body must not be read into memory without a cap."""
    total = 256 * 1024 * 1024
    chunk = b" " * (1024 * 1024)
    sent: list[int] = []

    async def flood(target: str, headers: dict[str, str], writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
        count = 0
        try:
            while count < total:
                writer.write(chunk)
                await writer.drain()
                count += len(chunk)
        finally:
            sent.append(count)

    async def scenario() -> None:
        async with LoopbackServer(flood).running() as server, graph_service(tmp_path) as service:
            await add_agent(service, "flood", f"http://127.0.0.1:{server.port}/v1")
            await asyncio.wait_for(service.poll_endpoints(), 60)
            assert sent, "the poller never probed the endpoint"
            read = max(sent)
            assert read < 16 * 1024 * 1024, f"a single probe read {read // (1024 * 1024)} MiB of response body"

    asyncio.run(scenario())


def test_a_slow_dripping_endpoint_does_not_stall_the_pass_for_every_other_agent(tmp_path: Path) -> None:
    """One endpoint that answers a byte at a time, each within httpx's 5s read timeout,
    must not hold the pass -- and so every other agent's health -- for as long as it likes."""

    async def drip(target: str, headers: dict[str, str], writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
        await writer.drain()
        for _ in range(600):
            writer.write(b" ")
            await writer.drain()
            await asyncio.sleep(1)

    async def scenario() -> None:
        slow, live = LoopbackServer(drip), LoopbackServer(ollama_running_qwen)
        async with slow.running(), live.running(), graph_service(tmp_path) as service:
            await add_agent(service, "a-slow", f"http://127.0.0.1:{slow.port}/v1")
            await add_agent(service, "b-live", f"http://127.0.0.1:{live.port}/v1")
            started = time.monotonic()
            try:
                await asyncio.wait_for(service.poll_endpoints(), 20)
            except TimeoutError:
                pytest.fail(f"one dripping endpoint held the poll pass for over {time.monotonic() - started:.0f}s")
            assert (await served_health(service, "b-live"))["modelLoaded"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:169.254.169.254]/latest",  # the IMDS address, IPv4-mapped
        "http://[fd00:ec2::254]/latest",  # AWS IMDS over IPv6
        "http://169.254.170.2/v2",  # ECS task metadata
        "http://169.254.169.254./latest",  # the IMDS address as a fully qualified name
        "http://metadata.google.internal/computeMetadata/v1",  # GCP metadata by name
    ],
)
def test_the_poller_does_not_probe_the_metadata_service_by_another_spelling(tmp_path: Path, url: str) -> None:
    """Refusing the literal 169.254.169.254 alone is not a block: a client that adds an agent
    with another spelling of the metadata address, another metadata endpoint, or a name for one
    still gets the daemon to probe it every pass and report whether it answered."""
    probed: list[str] = []

    def metadata_service(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        return httpx.Response(200, json={"models": []})

    async def scenario() -> None:
        async with graph_service(tmp_path, endpoint_transport=httpx.MockTransport(metadata_service)) as service:
            await add_agent(service, "imds", url)
            await service.poll_endpoints()
            assert probed == [], f"the poller requested the metadata service on its own: {probed}"
            assert await served_health(service, "imds") is None

    asyncio.run(scenario())
