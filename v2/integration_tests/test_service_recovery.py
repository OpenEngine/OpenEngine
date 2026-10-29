"""GE and VCP keep authorization and shutdown correct across real broker outages."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import nats
import pytest
from openengine.messaging import Route, Server
from openengine.supervisor.processes.nats import install_nats_server
from virtual_control_plane.exclusion import StateDirectory

pytestmark = pytest.mark.integration


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(predicate, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("condition did not become true before the deadline")


def _health(url: str) -> int | None:
    try:
        with urlopen(url, timeout=0.5) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, TimeoutError):
        return None


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def nats_binary() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openengine"
    return install_nats_server(cache)


@pytest.fixture
def service_hook(request):
    return getattr(request, "param", None)


@pytest.fixture(params=[("ge", "graph_engine", "graphs.list"), ("vcp", "virtual_control_plane", "capabilities.get")])
def service(request, tmp_path: Path, nats_binary: Path, service_hook):
    name, module, operation = request.param
    port = _port()
    children = []
    files = []

    def spawn(arguments, environment=None):
        log = (tmp_path / f"process-{len(children)}.log").open("w")
        files.append(log)
        process = subprocess.Popen(arguments, env=environment, stdout=log, stderr=subprocess.STDOUT)
        children.append(process)
        return process

    def broker(host="127.0.0.1"):
        process = spawn([str(nats_binary), "-a", host, "-p", str(port)])

        def listening():
            assert process.poll() is None, "fixture broker exited"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False

        _wait(listening)
        return process

    ready = tmp_path / "ready"
    state = tmp_path / "vcp"
    environment = {
        **os.environ, "XDG_STATE_HOME": str(tmp_path / "state"),
        "OE_NATS_URL": f"nats://127.0.0.1:{port}", "OE_VCP_STATE_DIR": str(state),
        f"OE_{name.upper()}_READY_FILE": str(ready), f"OE_{name.upper()}_HEALTH_PORT": "0",
    }

    async def query():
        connection = await nats.connect(f"nats://127.0.0.1:{port}", max_reconnect_attempts=0)
        try:
            reply = await connection.request(f"oe.v1.{name}.query.{operation}", b'{"params": {}}', timeout=3)
            return json.loads(reply.data)
        finally:
            await connection.close()

    try:
        initial_broker = broker()
        arguments = [sys.executable, "-m", module]
        if service_hook:
            fixture = Path(__file__).parent / "fixtures" / "shutdown_service.py"
            arguments = [sys.executable, str(fixture), module, operation, service_hook, str(tmp_path)]
        process = spawn(arguments, environment)
        _wait(lambda: ready.exists())
        url = ready.read_text()
        _wait(lambda: _health(url) == 200)
        yield name, process, initial_broker, broker, query, url, ready, state, f"nats://127.0.0.1:{port}"
    finally:
        for process in reversed(children):
            _stop(process)
        for log in files:
            log.close()


def test_reconnect_rechecks_current_broker_permissions(service) -> None:
    _, process, broker, restart, query, url, _, _, _ = service
    assert asyncio.run(query())["status"] == "ok"
    _stop(broker)
    _wait(lambda: _health(url) == 503)
    broker = restart("0.0.0.0")
    _wait(lambda: _health(url) == 200)
    reply = asyncio.run(query())
    assert reply.get("error", {}).get("code") == "PERMISSION_DENIED", reply
    assert process.poll() is None
    _stop(broker)
    _wait(lambda: _health(url) == 503)
    restart()
    _wait(lambda: _health(url) == 200)
    assert asyncio.run(query())["status"] == "ok"


def test_sigterm_during_broker_outage_cleans_up(service) -> None:
    name, process, broker, _, _, url, ready, state, _ = service
    _stop(broker)
    _wait(lambda: _health(url) == 503)
    process.terminate()
    assert process.wait(timeout=8) == 0
    assert not ready.exists()
    assert _health(url) is None
    if name == "vcp":
        with StateDirectory(state):
            pass  # The stopped process no longer owns the workspace state directory.


@pytest.mark.parametrize("finish_handler", [True, False], ids=["pending-reply", "stuck-handler"])
def test_shared_shutdown_drains_or_times_out(broker_url, finish_handler: bool) -> None:
    async def scenario() -> None:
        url = broker_url
        connection = await nats.connect(url)
        client = await nats.connect(url)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return {"completed": True}

        server = Server(connection, "shutdown-test", url, queries={"check": Route(handler)})
        await server.start()
        pending = asyncio.create_task(client.request("oe.v1.shutdown-test.query.check", b'{"params": {}}', timeout=3))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            closing = asyncio.create_task(server.close(timeout=0.3))
            await asyncio.sleep(0.05)
            assert not closing.done(), "shutdown must wait for a reply already being handled"
            if finish_handler:
                release.set()
                reply = await pending
                assert json.loads(reply.data)["result"] == {"completed": True}
            await asyncio.wait_for(closing, timeout=1)
            assert connection.is_closed
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await connection.close()
            await client.close()

    asyncio.run(scenario())


@pytest.fixture
def broker_url(tmp_path, nats_binary):
    port = _port()
    with (tmp_path / "broker.log").open("w") as log:
        process = subprocess.Popen([str(nats_binary), "-a", "127.0.0.1", "-p", str(port)], stdout=log, stderr=log)
        try:
            def listening():
                assert process.poll() is None
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return True
                except OSError:
                    return False
            _wait(listening)
            yield f"nats://127.0.0.1:{port}"
        finally:
            _stop(process)


@pytest.mark.parametrize("phase", ["drain", "wait_closed", "cancel-suppressed"])
def test_close_deadline_aborts_stalled_transport(broker_url, monkeypatch, phase):
    async def scenario():
        connection = await nats.connect(broker_url)
        transport = connection._transport
        entered = asyncio.Event()

        async def stall():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if phase != "cancel-suppressed":
                    raise
                # Model Client.close() swallowing cancellation during reconnect.
                await asyncio.Event().wait()

        monkeypatch.setattr(transport, "wait_closed" if phase == "cancel-suppressed" else phase, stall)
        if phase == "drain":
            # Leave bytes for Client.close() to flush, after its flusher has stopped.
            connection._flusher_task.cancel()
            await asyncio.gather(connection._flusher_task, return_exceptions=True)
            connection._pending = [b"PING\r\n"]
            connection._pending_data_size = 6
        server = Server(connection, "shutdown-test", broker_url)
        # Skip subscription drain so the injected stall is in Client.close().
        monkeypatch.setattr(server, "_drain_connection", lambda: asyncio.sleep(0))
        task = asyncio.create_task(server.close(timeout=0.1))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            done, _ = await asyncio.wait([task], timeout=1)
            assert task in done, "final connection close exceeded the shutdown budget"
            with pytest.raises(TimeoutError):
                task.result()
            assert transport._io_writer.transport.is_closing()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            transport._io_writer.transport.abort()
    asyncio.run(scenario())


@pytest.mark.parametrize("service_hook", ["pending"], indirect=True)
@pytest.mark.parametrize("finish_handler", [True, False], ids=["reply", "cancel"])
def test_sigterm_during_active_request(service, tmp_path, finish_handler):
    name, process, _, _, _, health, ready, state, url = service
    operation = "capabilities.get" if name == "vcp" else "graphs.list"

    async def scenario():
        client = await nats.connect(url)
        pending = asyncio.create_task(client.request(f"oe.v1.{name}.query.{operation}", b'{"params": {}}', timeout=10))
        try:
            await asyncio.to_thread(_wait, lambda: (tmp_path / "entered").exists())
            process.terminate()
            await asyncio.to_thread(_wait, lambda: _health(health) == 503)
            assert not ready.exists(), "readiness must be withdrawn before waiting for the handler"
            if name == "vcp":
                from virtual_control_plane.exclusion import AlreadyRunning
                with pytest.raises(AlreadyRunning):
                    StateDirectory(state).acquire()
            if finish_handler:
                (tmp_path / "release").touch()
                assert json.loads((await pending).data)["status"] == "ok"
            assert await asyncio.to_thread(process.wait, timeout=8) == 0
            assert (tmp_path / "handler-stopped").exists()
            assert _health(health) is None
            if name == "vcp":
                with StateDirectory(state):
                    pass
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("service_hook", ["stalled-close", "close-error"], indirect=True)
def test_failed_close_exits_nonzero_and_releases_resources(service, tmp_path, service_hook):
    name, process, _, _, _, health, ready, state, _ = service
    process.terminate()
    assert process.wait(timeout=8) == 1
    if service_hook == "stalled-close":
        assert (tmp_path / "closing").exists()
    assert not ready.exists()
    assert _health(health) is None
    if name == "vcp":
        with StateDirectory(state):
            pass
