import asyncio
import runpy
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_verify.artifacts import Artifacts
from open_verify.auth import AssistedLogin, LoginRequest
from open_verify.browser_session import BrowserSession
from open_verify.playwright_runner import PlaywrightRunner
from open_verify.test_spec import BrowserTest


@pytest.mark.parametrize('shared', [False, True])
def test_assisted_login_reuses_private_state_and_detects_expiry(tmp_path, monkeypatch, shared):
    pytest.importorskip('playwright.async_api')
    from playwright.async_api import BrowserType

    from open_verify.tools import LocalTools

    launches = []
    original_launch = BrowserType.launch

    async def launch(self, **kwargs):
        launches.append(kwargs)
        return await original_launch(self, **{**kwargs, 'headless': True})

    monkeypatch.setattr(BrowserType, 'launch', launch)

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
    session = BrowserSession() if shared else None
    login = AssistedLogin(tmp_path, progress=lambda _: None, browser_session=session)
    runner = PlaywrightRunner(tmp_path, artifacts, browser_session=session)
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
        if shared:
            assert session.browser.is_connected()
            assert session.browser.contexts == []
        assert len(launches) == (1 if shared else 5)

    async def run_and_close():
        try:
            await run()
        finally:
            if session:
                assert await session.close() == []
                assert session.browser is None
                assert session.playwright is None
    try:
        asyncio.run(run_and_close())
        if shared:
            # Replay the exported authenticated test using a fresh assisted login,
            # without exporting or asking the reviewer to construct a cookie file.
            valid = True
            script = sorted((artifacts.path / 'tests').glob('test_*_001.py'))[0]
            monkeypatch.setattr(sys, 'argv', [str(script), '--login', '--output', str(tmp_path / 'replay')])
            monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
            with pytest.raises(SystemExit) as result:
                runpy.run_path(str(script), run_name='__main__')
            assert result.value.code == 0
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


def test_assisted_login_waits_through_internal_pages(tmp_path, monkeypatch):
    from open_verify.tools import LocalTools

    urls = iter(('about:blank', 'chrome-error://chromewebdata/',
                 'https://github.com/login', 'https://accounts.google.com/v3/signin/identifier',
                 'https://github.com/session', 'http://localhost:5173/'))
    events = []

    class Page:
        @property
        def url(self):
            return next(urls)

        async def goto(self, url, **kwargs):
            events.append('open')

        def is_closed(self):
            return False

    class Context:
        async def storage_state(self):
            return {
                'cookies': [{'domain': '.google.com', 'name': 'provider', 'value': 'private'},
                            {'domain': '.github.com', 'name': 'provider', 'value': 'private'},
                            {'domain': 'localhost', 'name': 'session', 'value': 'app'}],
                'origins': [{'origin': 'https://accounts.google.com', 'localStorage': []},
                            {'origin': 'http://localhost:5173', 'localStorage': []}],
            }

    class FixtureTools(LocalTools):
        async def browser_page(self):
            self.check_url('https://accounts.google.com/v3/signin/identifier')
            self.check_url('https://www.gstatic.com/asset.js')
            self.check_url('https://ssl.gstatic.com/asset.js')
            with pytest.raises(ValueError, match='allow-origin'):
                self.check_url('https://accounts.google.com.example.org/')
            self.context = Context()
            return Page()

        def _locator(self, args):
            return self

        async def click(self):
            events.append('click')

        async def close(self):
            events.append('close')

    async def pause(_):
        assert 'close' not in events
        events.append('wait')

    async def confirm(context, request):
        events.append('confirmed')
        return True

    monkeypatch.setattr('open_verify.auth.LocalTools', FixtureTools)
    monkeypatch.setattr('open_verify.auth.asyncio.sleep', pause)
    login = AssistedLogin(tmp_path, progress=lambda _: None)
    monkeypatch.setattr(login, 'confirm', confirm)
    result = asyncio.run(login.run(LoginRequest(
        url='http://localhost:5173', status_url='http://localhost:5173/status',
        login={'by': 'text', 'name': 'Sign in with GitHub'},
    )))
    assert result['authenticated'] is True
    assert events == ['open', 'click', *(['wait'] * 5), 'confirmed', 'close']
    assert login.state == {
        'cookies': [{'domain': 'localhost', 'name': 'session', 'value': 'app'}],
        'origins': [{'origin': 'http://localhost:5173', 'localStorage': []}],
    }
    ordinary = LocalTools(tmp_path, Artifacts(tmp_path / 'ordinary'))
    with pytest.raises(ValueError, match='allow-origin'):
        ordinary.check_url('https://accounts.google.com/')
