import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_verify.artifacts import Artifacts
from open_verify.tools import LocalTools, project_root


@pytest.fixture
def web_app():
    class App(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://example.com")
                self.end_headers()
                return
            body = b"""<html><body><label>Name<input id="name"></label>
                <button onclick="document.getElementById('result').textContent='Hello '+document.getElementById('name').value">Greet</button>
                <p id="result"></p></body></html>"""
            if self.path == "/api":
                body = json.dumps({"items": [1, 2]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_commands_capture_stdin_nonzero_exit_and_timeouts(tmp_path):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), allow_exec=True)
        try:
            result = await tools.execute(
                "run_command",
                {
                    "argv": [sys.executable, "-c", "import sys; print(input()); sys.exit(3)"],
                    "stdin": "hello\n",
                },
            )
            assert result["result"]["exit_code"] == 3
            assert "hello" in result["result"]["output"]
            result = await tools.execute(
                "run_command",
                {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 0.1},
            )
            assert result["result"]["timed_out"]
            assert result["result"]["exit_code"] is not None
        finally:
            assert await tools.close() == []

    asyncio.run(run())


def test_managed_service_is_stopped_on_cleanup(tmp_path):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"), allow_exec=True)
        await tools.execute(
            "start_process", {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
        )
        process = tools.processes["P001"][0]
        assert process.returncode is None
        assert await tools.close() == []
        assert process.returncode is not None

    asyncio.run(run())


def test_bounded_wait_is_recorded(tmp_path):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"))
        result = await tools.execute("wait", {"seconds": 0.01})
        assert result["ok"]
        assert result["result"]["seconds"] == 0.01

    asyncio.run(run())


def test_scope_and_execution_controls(tmp_path):
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "src"
    subdir.mkdir()
    assert project_root(subdir) == tmp_path
    tools = LocalTools(tmp_path, Artifacts(tmp_path / "runs"))
    for path in ["../outside", ".env", ".git/config", "private.key"]:
        with pytest.raises(ValueError):
            tools.path(path)
    tools.check_url("http://localhost:8000")
    with pytest.raises(ValueError):
        tools.check_url("https://example.com")
    with pytest.raises(ValueError):
        tools.check_url("http://user:password@localhost:8000")
    result = asyncio.run(
        tools.execute("run_command", {"argv": [sys.executable, "-c", "print('no')"]})
    )
    assert not result["ok"]
    assert "--allow-exec" in result["result"]["error"]


def test_http_evidence_and_redirect_boundary(tmp_path, web_app):
    async def run():
        artifacts = Artifacts(tmp_path / "runs")
        tools = LocalTools(tmp_path, artifacts)
        result = await tools.execute("http_request", {"url": web_app + "/api"})
        assert result["result"]["status"] == 200
        assert json.loads(result["result"]["body"]) == {"items": [1, 2]}
        receipt = artifacts.path / result["artifact"]
        assert json.loads(receipt.read_text())["arguments"]["url"] == web_app + "/api"
        assert json.loads((artifacts.path / result["result"]["body_file"]).read_text()) == {
            "items": [1, 2]
        }
        result = await tools.execute("http_request", {"url": web_app + "/redirect"})
        assert result["result"]["status"] == 302

    asyncio.run(run())


def test_real_browser_form_flow(tmp_path, web_app):
    pytest.importorskip("playwright.async_api")

    async def run():
        artifacts = Artifacts(tmp_path / "runs")
        tools = LocalTools(tmp_path, artifacts, headless=True)
        try:
            result = await tools.execute("browser_open", {"url": web_app})
            if not result["ok"] and "Executable doesn't exist" in result["result"]["error"]:
                pytest.skip("Chromium has not been installed")
            assert result["ok"], result
            result = await tools.execute(
                "browser_fill", {"by": "label", "name": "Name", "value": "Ada"}
            )
            assert result["ok"], result
            result = await tools.execute(
                "browser_click", {"by": "role", "role": "button", "name": "Greet"}
            )
            assert result["ok"], result
            result = await tools.execute("browser_expect_text", {"text": "Hello Ada"})
            assert result["ok"], result
            assert "Hello Ada" in result["result"]["snapshot"]
            assert (artifacts.path / result["result"]["screenshot"]).is_file()
        finally:
            assert await tools.close() == []
        assert (artifacts.path / "browser-trace.zip").is_file()

    asyncio.run(run())
