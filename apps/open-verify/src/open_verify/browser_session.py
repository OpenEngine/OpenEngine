"""One Chromium process per run; callers own their isolated contexts."""

import asyncio


class BrowserSession:
    def __init__(self, *, headless=False):
        self.headless = headless
        self.browser = None
        self.playwright = None
        self._lock = asyncio.Lock()

    async def get_browser(self):
        async with self._lock:
            if self.browser is not None:
                if not self.browser.is_connected():
                    raise RuntimeError("The run's browser was closed; restart verification")
                return self.browser
            from playwright.async_api import async_playwright

            try:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=self.headless,
                    # Every caller supplies its own guarded context proxy. A
                    # context accidentally created without one must fail closed.
                    proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                )
                return self.browser
            except BaseException:
                await self.close()
                raise

    async def close(self):
        errors = []
        for name, operation in (
            ("browser", lambda: self.browser.close() if self.browser else None),
            ("playwright", lambda: self.playwright.stop() if self.playwright else None),
        ):
            try:
                pending = operation()
                if pending is not None:
                    await pending
                setattr(self, name, None)
                if name == "playwright":
                    self.browser = None
            except Exception as exc:
                errors.append(f"shared {name}: {exc}")
        return errors
