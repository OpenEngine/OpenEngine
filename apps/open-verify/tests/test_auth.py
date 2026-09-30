import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_verify.artifacts import Artifacts
from open_verify.auth import AssistedLogin, LoginRequest
from open_verify.playwright_runner import PlaywrightRunner
from open_verify.test_spec import BrowserTest


def test_assisted_login_reuses_private_state_and_detects_expiry(tmp_path, monkeypatch):
    pytest.importorskip('playwright.async_api')
    from open_verify.tools import LocalTools

    class FixtureTools(LocalTools):
        def __init__(self, *args, **kwargs):
            assert kwargs['headless'] is False  # Production assisted login is visible.
            assert kwargs['trace_browser'] is False
            kwargs['headless'] = True
            super().__init__(*args, **kwargs)

    monkeypatch.setattr('open_verify.auth.LocalTools', FixtureTools)
    valid = True

    class App(BaseHTTPRequestHandler):
        def do_GET(self):
            signed_in = valid and 'session=private-fixture-cookie' in self.headers.get('Cookie', '')
            if self.path == '/authorize':
                self.send_response(302)
                self.send_header('Set-Cookie', 'session=private-fixture-cookie; HttpOnly; Path=/')
                self.send_header('Location', '/')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == '/status' else 'text/html')
            self.end_headers()
            if self.path == '/status':
                self.wfile.write(b'{"authenticated":true}' if signed_in else b'{"authenticated":false}')
            else:
                self.wfile.write(b'<p>Private dashboard</p>' if signed_in else b'<a href="/authorize">Sign in with GitHub</a>')
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    artifacts = Artifacts(tmp_path / 'runs')
    login = AssistedLogin(tmp_path, progress=lambda _: None)
    runner = PlaywrightRunner(tmp_path, artifacts)
    runner.authentication = login

    async def run():
        nonlocal valid
        result = await login.run(LoginRequest(url=url, status_url=url+'/status',
            login={'by':'role','role':'link','name':'Sign in with GitHub'}, timeout=15))
        assert result['authenticated'] is True
        assert 'private-fixture-cookie' not in str(result)
        for case in ('first', 'second'):
            result = await runner.run(BrowserTest(case_id=case, url=url, authenticated=True,
                steps=[{'kind':'expect_text','text':'Private dashboard'}]), capture_media=False)
            assert result.status == 'passed', result.detail
        signed_out = await runner.run(BrowserTest(case_id='signed-out', url=url,
            steps=[{'kind':'expect_text','text':'Sign in with GitHub'}]), capture_media=False)
        assert signed_out.status == 'passed'
        valid = False
        expired = await runner.run(BrowserTest(case_id='expired', url=url, authenticated=True,
            steps=[{'kind':'expect_text','text':'Private dashboard'}]), capture_media=False)
        assert expired.status == 'blocked'
        assert 'expired' in expired.detail
    try:
        asyncio.run(run())
        for file in artifacts.path.rglob('*'):
            if file.is_file() and file.suffix in {'.py', '.json', '.md'}:
                assert 'private-fixture-cookie' not in file.read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_assisted_login_rejects_external_status_endpoint(tmp_path):
    login = AssistedLogin(tmp_path)
    request = LoginRequest(url='http://localhost:8000', status_url='https://example.com/status',
        login={'by':'text','name':'Sign in'})
    with pytest.raises(ValueError, match='same origin'):
        asyncio.run(login.run(request))
