"""Regression tests for origin enforcement, deadlines, and browser ownership."""

import asyncio
import base64
import hashlib
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import pytest
from network_fixtures import external_destination

from open_verify.artifacts import Artifacts
from open_verify.browser_guard import ChildSession
from open_verify.tools import LocalTools


@contextmanager
def serve(handler, host="127.0.0.1"):
    server = ThreadingHTTPServer((host, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def redirect(self, location, status=302):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def html(self, body):
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def site_isolated_browser(monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    launch = playwright.BrowserType.launch

    async def isolated(browser_type, **kwargs):
        kwargs["args"] = [*kwargs.get("args", []), "--site-per-process"]
        return await launch(browser_type, **kwargs)

    monkeypatch.setattr(playwright.BrowserType, "launch", isolated)


@pytest.mark.parametrize("trigger", ["navigation", "click", "fetch", "iframe", "nested_iframe"])
def test_browser_blocks_redirect_before_destination_receives_request(
    tmp_path, trigger, site_isolated_browser, monkeypatch
):
    pytest.importorskip("playwright.async_api")
    received = []

    class Denied(QuietHandler):
        def do_GET(self):
            received.append(self.path)
            self.html("Rejected destination")

    with serve(Denied) as denied_url:
        external_destination(monkeypatch, denied_url)

        class Allowed(QuietHandler):
            def do_GET(self):
                if self.path == "/first":
                    self.redirect("/second", 307)
                elif self.path == "/second":
                    self.redirect(denied_url + "/must-not-arrive", 302)
                elif self.path == "/frame":
                    self.html(
                        """<script>fetch('/first').then(() => parent.postMessage('Unexpected success', '*')).catch(() => parent.postMessage('Fetch blocked', '*'))</script>"""
                    )
                elif self.path == "/outer-frame":
                    self.html(
                        f"""<script>onmessage = event => parent.postMessage(event.data, '*')</script><iframe src="http://127.0.0.1:{self.server.server_port}/frame"></iframe>"""
                    )
                elif trigger in {"iframe", "nested_iframe"}:
                    frame_path = "/frame" if trigger == "iframe" else "/outer-frame"
                    self.html(
                        f"""<p></p><script>onmessage = event => document.querySelector('p').textContent = event.data</script><iframe src="http://localhost:{self.server.server_port}{frame_path}"></iframe>"""
                    )
                else:
                    self.html("""<a href="/first">Follow redirect</a>
                        <button onclick="fetch('/first').then(() => document.querySelector('p').textContent='Unexpected success').catch(() => document.querySelector('p').textContent='Fetch blocked')">Fetch redirect</button><p></p>""")

        with serve(Allowed) as allowed_url:

            async def run():
                tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), headless=True)
                try:
                    opened = await tools.execute("browser_open", {"url": allowed_url})
                    if not opened["ok"] and "Executable doesn't exist" in opened["result"]["error"]:
                        pytest.skip("Chromium has not been installed")
                    assert opened["ok"], opened
                    if trigger == "navigation":
                        result = await tools.execute(
                            "browser_open", {"url": allowed_url + "/first"}
                        )
                        assert not result["ok"], result
                    elif trigger == "click":
                        await tools.execute(
                            "browser_click",
                            {"by": "role", "role": "link", "name": "Follow redirect"},
                        )
                    else:
                        if trigger == "fetch":
                            await tools.execute(
                                "browser_click",
                                {"by": "role", "role": "button", "name": "Fetch redirect"},
                            )
                        result = await tools.execute(
                            "browser_expect_text", {"text": "Fetch blocked"}
                        )
                        assert result["ok"], result
                    assert received == [], "A forbidden redirected request reached the server"
                    if trigger in {"iframe", "nested_iframe"}:
                        minimum = 2 if trigger == "nested_iframe" else 1
                        assert tools._browser_guard.protected_targets.count("iframe") >= minimum
                finally:
                    await tools.close()

            asyncio.run(run())


@pytest.mark.parametrize(
    "target", ["page", "iframe", "worker", "nested_worker", "blob_worker", "shared_worker"]
)
@pytest.mark.parametrize("policy", ["denied", "explicit", "localhost"])
def test_websocket_origin_checked_before_handshake(
    tmp_path, site_isolated_browser, target, policy, monkeypatch
):
    if "worker" in target:
        send = ChildSession.send

        async def slow_setup(session, method, params=None):
            if method == "Target.setAutoAttach":
                await asyncio.sleep(0.2)
            return await send(session, method, params)

        # Worker code can run before a second CDP client finishes attaching.
        # Origin enforcement must already apply to the very first handshake.
        monkeypatch.setattr(ChildSession, "send", slow_setup)
    handshakes = []

    class WebSocketServer(QuietHandler):
        def do_GET(self):
            handshakes.append(self.path)
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            # A complete unmasked server text frame. Verify it reaches the page
            # through the route, in addition to observing the opening handshake.
            self.wfile.write(b"\x81\x07allowed")
            self.wfile.flush()
            self.connection.settimeout(2)
            try:
                self.rfile.read(2)
            except OSError:
                pass

    host = "127.0.0.1"
    with serve(WebSocketServer, host) as websocket_origin:
        if policy != "localhost":
            external_destination(monkeypatch, websocket_origin)
        websocket_url = websocket_origin.replace("http:", "ws:") + "/socket"

        class Page(QuietHandler):
            def do_GET(self):
                if "worker" in target:
                    script = f"""try {{const socket = new WebSocket({websocket_url!r});
                        let delivered = false;
                        socket.onmessage = e => {{delivered = true; postMessage(e.data);}};
                        socket.onclose = () => {{if (!delivered) postMessage('blocked');}};
                        socket.onerror = () => postMessage('blocked');
                        }} catch(e) {{postMessage('blocked');}}"""
                    if target == "shared_worker":
                        script = (
                            "onconnect = e => { const postMessage = text => e.ports[0].postMessage(text); "
                            + script
                            + " };"
                        )
                    if self.path == "/nested.js":
                        script = 'const child = new Worker("/worker.js"); child.onmessage = e => postMessage(e.data);'
                    if self.path.endswith(".js"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/javascript")
                        self.end_headers()
                        self.wfile.write(script.encode())
                    else:
                        constructor = {
                            "worker": "new Worker('/worker.js')",
                            "nested_worker": "new Worker('/nested.js')",
                            "shared_worker": "new SharedWorker('/worker.js').port",
                            "blob_worker": f"new Worker(URL.createObjectURL(new Blob([{script!r}], {{type:'text/javascript'}})))",
                        }[target]
                        self.html(f"""<p></p><script>const w={constructor};
                            w.onmessage=e=>document.querySelector('p').textContent=e.data;</script>""")
                elif target == "iframe" and self.path != "/frame":
                    self.html(
                        f"""<p></p><script>onmessage = e => document.querySelector('p').textContent=e.data;</script><iframe src="http://localhost:{self.server.server_port}/frame"></iframe>"""
                    )
                else:
                    self.html(f"""<p></p><script>
                        const show = text => {{document.querySelector('p').textContent=text; parent.postMessage(text, '*');}};
                        const socket = new WebSocket({websocket_url!r});
                        socket.onmessage = e => show(e.data);
                        socket.onclose = () => {{if (!document.querySelector('p').textContent) show('blocked');}};
                        socket.onerror = () => show('blocked');
                    </script>""")

        with serve(Page) as page_url:

            async def run():
                tools = LocalTools(
                    tmp_path,
                    Artifacts(tmp_path / "runs"),
                    headless=True,
                    allow_origins=[websocket_origin] if policy == "explicit" else [],
                )
                try:
                    result = await tools.execute("browser_open", {"url": page_url})
                    if not result["ok"] and "Executable doesn't exist" in result["result"]["error"]:
                        pytest.skip("Chromium has not been installed")
                    assert result["ok"], result
                    expected = "blocked" if policy == "denied" else "allowed"
                    result = await tools.execute("browser_expect_text", {"text": expected})
                    assert result["ok"], result
                    assert handshakes == ([] if policy == "denied" else ["/socket"])
                    if target == "iframe":
                        assert "iframe" in tools._browser_guard.protected_targets
                finally:
                    assert await tools.close() == []

            asyncio.run(run())


def test_websocket_scheme_and_port_policy(tmp_path):
    tools = LocalTools(
        tmp_path,
        Artifacts(tmp_path / "runs"),
        allow_origins=["https://example.com:9443", "http://example.com"],
    )
    tools.check_websocket_url("wss://example.com:9443/socket")
    tools.check_websocket_url("ws://example.com:80/socket")
    tools.check_websocket_url("ws://localhost:8000/socket")
    for url in [
        "wss://example.com/socket",
        "ws://example.com:9443/socket",
        "ws://user:password@example.com/socket",
        "https://example.com/socket",
    ]:
        with pytest.raises(ValueError):
            tools.check_websocket_url(url)


def test_child_target_stays_paused_if_interceptor_installation_fails(
    tmp_path, monkeypatch, site_isolated_browser
):
    escaped = []
    send = ChildSession.send

    async def fail_interception(session, method, params=None):
        if method == "Fetch.enable":
            # Leave enough time for an incorrectly resumed frame to run its
            # very first script; an unprotected target must stay paused here.
            await asyncio.sleep(0.2)
            raise RuntimeError("Injected child interception failure")
        return await send(session, method, params)

    monkeypatch.setattr(ChildSession, "send", fail_interception)

    class App(QuietHandler):
        def do_GET(self):
            if self.path == "/frame":
                self.html("<script>fetch('/escaped')</script>")
            elif self.path == "/escaped":
                escaped.append(self.path)
                self.html("Should never arrive")
            else:
                self.html(
                    f'<iframe src="http://localhost:{self.server.server_port}/frame"></iframe>'
                )

    with serve(App) as url:

        async def run():
            tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), headless=True)
            try:
                await tools._ensure_browser()
                closed = asyncio.Event()
                tools.context.on("close", lambda: closed.set())
                await tools.execute("browser_open", {"url": url})
                await asyncio.wait_for(closed.wait(), 5)
                assert escaped == []
                assert any(
                    "Injected child interception failure" in event.get("interception_error", "")
                    for event in tools.browser_events
                )
            finally:
                await tools.close()

        asyncio.run(run())


def test_http_timeout_bounds_a_response_that_keeps_delivering_bytes(tmp_path):
    sent = []

    class Trickle(QuietHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "40")
            self.end_headers()
            for _ in range(40):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    sent.append(1)
                except OSError:
                    break
                time.sleep(0.05)

    with serve(Trickle) as url:

        async def run():
            tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"))
            start = time.monotonic()
            result = await tools.execute("http_request", {"url": url, "timeout": 0.3})
            elapsed = time.monotonic() - start
            assert not result["ok"], result
            assert "deadline" in result["result"]["error"].lower()
            assert elapsed < 1.2, elapsed
            assert len(sent) >= 2, (
                "The server must deliver data to distinguish total and idle timeouts"
            )

        asyncio.run(run())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_browser_follows_allowed_redirects_without_replaying_requests(tmp_path, status, monkeypatch):
    pytest.importorskip("playwright.async_api")
    requests = []

    class Destination(QuietHandler):
        def do_GET(self):
            requests.append(("GET", self.path, b""))
            self.html("Redirect complete")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(("POST", self.path, body))
            self.html("Redirect complete")

    with serve(Destination) as destination:
        external_destination(monkeypatch, destination)

        class Source(QuietHandler):
            def do_GET(self):
                self.html(
                    '<form action="/submit" method="post"><input name="value" value="test"><button>Submit</button></form>'
                )

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(("POST", self.path, body))
                self.redirect(destination + "/complete", status)

        with serve(Source) as source:

            async def run():
                tools = LocalTools(
                    tmp_path,
                    Artifacts(tmp_path / "runs"),
                    headless=True,
                    allow_origins=[destination],
                )
                try:
                    opened = await tools.execute("browser_open", {"url": source})
                    if not opened["ok"] and "Executable doesn't exist" in opened["result"]["error"]:
                        pytest.skip("Chromium has not been installed")
                    assert opened["ok"], opened
                    result = await tools.execute(
                        "browser_click", {"by": "role", "role": "button", "name": "Submit"}
                    )
                    assert result["ok"], result
                    result = await tools.execute(
                        "browser_expect_text", {"text": "Redirect complete"}
                    )
                    assert result["ok"], result
                    assert result["result"]["url"] == destination + "/complete"
                    assert requests == [
                        ("POST", "/submit", b"value=test"),
                        ("POST", "/complete", b"value=test")
                        if status in {307, 308}
                        else ("GET", "/complete", b""),
                    ]
                finally:
                    assert await tools.close() == []

            asyncio.run(run())


@pytest.mark.parametrize("stage", ["launch", "new_context", "new_page"])
def test_repeated_initialization_failure_stops_every_playwright_driver(
    tmp_path, monkeypatch, stage
):
    playwright = pytest.importorskip("playwright.async_api")
    drivers = []

    async def fail_initialization(*_args, **_kwargs):
        drivers.extend(
            child
            for child in psutil.Process().children(recursive=True)
            if child.name().lower() in {"node", "node.exe"}
        )
        raise RuntimeError("Simulated browser initialization failure")

    owner = {
        "launch": playwright.BrowserType,
        "new_context": playwright.Browser,
        "new_page": playwright.BrowserContext,
    }[stage]
    monkeypatch.setattr(owner, stage, fail_initialization)

    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), headless=True)
        try:
            for _ in range(2):
                result = await tools.execute("browser_open", {"url": "http://localhost:12345"})
                assert not result["ok"]
                if "Executable doesn't exist" in result["result"]["error"]:
                    pytest.skip("Chromium has not been installed")
                assert "Simulated browser initialization failure" in result["result"]["error"]
                assert not any(driver.is_running() for driver in drivers)
            assert await tools.close() == []
            assert not any(driver.is_running() for driver in drivers)
        finally:
            await tools.close()
            # Keep the regression itself from leaking processes on a failing implementation.
            for driver in drivers:
                if driver.is_running():
                    driver.kill()
                    driver.wait(timeout=5)

    asyncio.run(run())


def test_browser_initialization_recovers_after_cancellation(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    launch = playwright.BrowserType.launch
    attempts = 0

    async def cancel_once(browser_type, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError()
        return await launch(browser_type, **kwargs)

    monkeypatch.setattr(playwright.BrowserType, "launch", cancel_once)

    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), headless=True)
        try:
            with pytest.raises(asyncio.CancelledError):
                await tools._ensure_browser()
            assert tools.playwright is tools.browser is tools.context is tools.page is None
            try:
                await tools._ensure_browser()
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Chromium has not been installed")
                raise
            assert tools.page is not None
            assert attempts == 2
        finally:
            assert await tools.close() == []
        assert await tools.close() == []

    asyncio.run(run())
