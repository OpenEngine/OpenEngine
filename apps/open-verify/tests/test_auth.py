import asyncio
import json
import runpy
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
                self.send_header('Location', provider_url + '/login?state=private-state')
                self.end_headers()
                return
            if self.path.startswith('/complete'):
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

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', url + '/complete?code=private-code')
            self.end_headers()

        def log_message(self, *_):
            pass

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    provider_url = f'http://127.0.0.1:{provider.server_port}'
    artifacts = Artifacts(tmp_path / 'runs')
    session = BrowserSession() if shared else None
    login = AssistedLogin(tmp_path, progress=lambda _: None, browser_session=session, artifacts=artifacts)
    runner = PlaywrightRunner(tmp_path, artifacts, browser_session=session)
    runner.authentication = login

    async def run():
        nonlocal valid
        result = await login.run(LoginRequest(url=url, status_url=url+'/status',
            login={'by':'role','role':'link','name':'Sign in with GitHub'}, timeout=15), capture_media=True)
        assert result['authenticated'] is True
        assert 'private-fixture-cookie' not in str(result)
        assert len(login.screenshots) == 3
        assert [Path(p).name for p in login.screenshots] == ['01-login-requested.png', '02-login-success.png', 'login-journey-summary.gif']
        gif = (artifacts.path / login.screenshots[-1]).read_bytes()
        assert gif.startswith(b'GIF89a') and len(gif) < 10_000_000
        receipt = json.loads((artifacts.path / result['receipt']).read_text())
        assert receipt['status'] == 'passed'
        events = [e['event'] for e in receipt['events']]
        assert events[0] == 'signed_out_confirmed'
        assert 'sign_in_clicked' in events and 'authenticated_return_confirmed' in events
        assert any(e.get('origin') == provider_url for e in receipt['events'])
        for case in ('first', 'second'):
            result = await runner.run(BrowserTest(case_id=case, url=url, authenticated=True,
                steps=[{'kind':'expect_text','text':'Private dashboard'}]), capture_media=case == 'first')
            assert result.status == 'passed', result.detail
            assert 'User-assisted login passed' in result.detail
            if case == 'first':
                assert result.screenshots[:2] == login.screenshots[:2]
                assert sum(p.endswith('.gif') for p in result.screenshots) == 1
                assert not result.videos
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
                assert 'private-code' not in file.read_text()
                assert 'private-state' not in file.read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        provider.shutdown()
        provider.server_close()
        provider_thread.join()


def test_assisted_login_rejects_external_status_endpoint(tmp_path):
    login = AssistedLogin(tmp_path)
    request = LoginRequest(url='http://localhost:8000', status_url='https://example.com/status',
        login={'by':'text','name':'Sign in'})
    with pytest.raises(ValueError, match='same origin'):
        asyncio.run(login.run(request))


@pytest.mark.parametrize('url', ['https://github.com/login', 'http://localhost/callback?code=secret',
                                'http://localhost/#access_token=secret'])
def test_checkpoints_never_save_provider_or_callback_screens(tmp_path, url):
    artifacts = Artifacts(tmp_path / 'runs')
    login = AssistedLogin(tmp_path, artifacts=artifacts)
    class Page:
        async def route(self, *_):
            pass

        async def unroute(self, *_):
            pass

        async def screenshot(self, **kwargs):
            pytest.fail('Must not capture this screen')

    page = Page()
    page.url = url
    request = LoginRequest(url='http://localhost/', status_url='http://localhost/status',
                           login={'by': 'text', 'name': 'Sign in'})
    asyncio.run(login.capture(page, request, 'login-001', 'private', {}))
    assert not list(artifacts.path.rglob('*.png'))
    assert login.omissions


def test_assisted_login_waits_through_internal_pages(tmp_path, monkeypatch):
    from open_verify.tools import LocalTools

    urls = iter(('about:blank', 'chrome-error://chromewebdata/',
                 'https://github.com/login', 'https://accounts.google.com/v3/signin/identifier',
                 'https://github.com/session', 'http://localhost:5173/'))
    events = []

    class Page:
        def on(self, *_):
            pass

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
    async def signed_out(*_):
        return False
    monkeypatch.setattr(login, 'status', signed_out)
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
