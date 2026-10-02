"""Worker targets have different CDP capabilities from page/iframe targets."""

import asyncio
import json

import pytest
from network_fixtures import external_destination
from test_network_lifecycle import QuietHandler, serve

from open_verify.artifacts import Artifacts
from open_verify.tools import LocalTools


@pytest.mark.parametrize("kind", ["classic", "module", "blob", "nested", "shared"])
def test_worker_startup_keeps_browser_usable(tmp_path, kind):
    pytest.importorskip("playwright.async_api")

    class App(QuietHandler):
        def do_GET(self):
            if self.path.endswith(".js"):
                if self.path == "/nested.js":
                    script = 'const child = new Worker("/worker.js"); child.onmessage = e => postMessage(e.data);'
                elif kind == "shared":
                    script = 'onconnect = e => e.ports[0].postMessage("Worker ready");'
                else:
                    script = 'postMessage("Worker ready");'
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript")
                self.end_headers()
                self.wfile.write(script.encode())
            else:
                constructor = {
                    "classic": 'new Worker("/worker.js")',
                    "module": 'new Worker("/worker.js", {type: "module"})',
                    "blob": 'new Worker(URL.createObjectURL(new Blob([\'postMessage("Worker ready")\'], {type:"text/javascript"})))',
                    "nested": 'new Worker("/nested.js")',
                    "shared": 'new SharedWorker("/worker.js")',
                }[kind]
                handle = "worker.port" if kind == "shared" else "worker"
                self.html(f"""<p id="status"></p><button onclick="document.querySelector('#status').textContent='Still usable'">Continue</button>
                    <script>const worker = {constructor};
                    {handle}.onmessage = e => document.querySelector('#status').textContent=e.data;</script>""")

    with serve(App) as url:

        async def run():
            tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), headless=True)
            try:
                result = await tools.execute("browser_open", {"url": url})
                if not result["ok"] and "Executable doesn't exist" in result["result"]["error"]:
                    pytest.skip("Chromium has not been installed")
                assert result["ok"], result
                result = await tools.execute("browser_expect_text", {"text": "Worker ready"})
                assert result["ok"], result
                result = await tools.execute(
                    "browser_click", {"by": "role", "role": "button", "name": "Continue"}
                )
                assert result["ok"], result
                assert (await tools.execute("browser_expect_text", {"text": "Still usable"}))["ok"]
                if kind != "shared":
                    assert tools._browser_guard.protected_targets.count("worker") >= (
                        2 if kind == "nested" else 1
                    )
            finally:
                assert await tools.close() == [], tools.browser_events

        asyncio.run(run())


@pytest.mark.parametrize("mode", ["direct", "redirect", "allowed"])
@pytest.mark.parametrize("kind", ["dedicated", "nested", "shared"])
def test_worker_network_origin_policy(tmp_path, mode, kind, monkeypatch):
    pytest.importorskip("playwright.async_api")
    received = []

    class Destination(QuietHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"Permitted response")

    with serve(Destination) as destination:
        external_destination(monkeypatch, destination)

        class App(QuietHandler):
            def do_GET(self):
                if self.path == "/worker.js":
                    url = "/redirect" if mode == "redirect" else destination + "/data"
                    script = f'fetch({json.dumps(url)}).then(r => r.text()).then(postMessage).catch(() => postMessage("Blocked"));'
                    if kind == "shared":
                        script = (
                            "onconnect = e => { const postMessage = text => e.ports[0].postMessage(text); "
                            + script
                            + " };"
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.end_headers()
                    self.wfile.write(script.encode())
                elif self.path == "/redirect":
                    self.redirect(destination + "/data")
                elif self.path == "/nested.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript")
                    self.end_headers()
                    self.wfile.write(
                        b'const child = new Worker("/worker.js"); child.onmessage = e => postMessage(e.data);'
                    )
                else:
                    constructor = (
                        'new SharedWorker("/worker.js").port'
                        if kind == "shared"
                        else (
                            'new Worker("/nested.js")'
                            if kind == "nested"
                            else 'new Worker("/worker.js")'
                        )
                    )
                    self.html(
                        f'<p></p><script>const w={constructor};w.onmessage=e=>document.querySelector("p").textContent=e.data;</script>'
                    )

        with serve(App) as url:

            async def run():
                tools = LocalTools(
                    tmp_path,
                    Artifacts(tmp_path / "runs"),
                    headless=True,
                    allow_origins=[destination] if mode == "allowed" else [],
                )
                try:
                    result = await tools.execute("browser_open", {"url": url})
                    assert result["ok"], result
                    result = await tools.execute(
                        "browser_expect_text",
                        {"text": "Permitted response" if mode == "allowed" else "Blocked"},
                    )
                    assert result["ok"], result
                    assert received == (["/data"] if mode == "allowed" else [])
                    if kind != "shared":
                        assert "worker" in tools._browser_guard.protected_targets
                finally:
                    assert await tools.close() == []

            asyncio.run(run())
