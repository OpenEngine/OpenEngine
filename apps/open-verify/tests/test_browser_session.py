import asyncio

import httpx
import pytest
import test_tools

from open_verify.artifacts import Artifacts
from open_verify.browser_session import BrowserSession
from open_verify.tools import LocalTools

web_app = test_tools.web_app


def test_shared_browser_keeps_context_proxies_separate(tmp_path, web_app):
    pytest.importorskip('playwright.async_api')

    class RestrictedTools(LocalTools):
        def check_url(self, url):
            super().check_url(url)
            if self.origin(url) not in self.origins:
                raise ValueError('Fixture origin is not allowed in this context')

    session = BrowserSession(headless=True)
    allowed = RestrictedTools(tmp_path, Artifacts(tmp_path / 'allowed'),
                              allow_origins=[web_app], browser_session=session)
    denied = RestrictedTools(tmp_path, Artifacts(tmp_path / 'denied'), browser_session=session)

    async def proxy_get(tools):
        # Exercise the actual per-context proxy without Playwright's page route
        # guard, as worker traffic must also be confined to its own policy.
        port = tools._browser_proxy.server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(proxy=f'http://127.0.0.1:{port}', trust_env=False) as client:
            return await client.get(web_app)

    async def run():
        try:
            page = await allowed.browser_page()
            await page.goto(web_app)
            await denied.browser_page()
            assert allowed.browser is denied.browser is session.browser
            assert allowed.context is not denied.context
            assert (await proxy_get(allowed)).status_code == 200
            assert (await proxy_get(denied)).status_code == 403
            assert await denied.close() == []
            assert session.browser.is_connected()
            assert (await proxy_get(allowed)).status_code == 200
        finally:
            assert await allowed.close() == []
            assert await denied.close() == []
            assert await session.close() == []

    asyncio.run(run())


def test_shared_browser_cleans_up_cancelled_launch(monkeypatch):
    api = pytest.importorskip('playwright.async_api')
    events = []

    class Driver:
        @property
        def chromium(self):
            return self

        async def launch(self, **kwargs):
            events.append('launch')
            raise asyncio.CancelledError()

        async def stop(self):
            events.append('stop')

        async def start(self):
            return self

    monkeypatch.setattr(api, 'async_playwright', Driver)
    session = BrowserSession()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.get_browser())
    assert session.browser is session.playwright is None
    assert events == ['launch', 'stop']
