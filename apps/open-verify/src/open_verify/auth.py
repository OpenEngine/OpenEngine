"""User-assisted browser authentication; state stays in memory for this run."""

import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from open_verify.artifacts import Artifacts
from open_verify.models import Contract
from open_verify.test_spec import Locator
from open_verify.tools import LocalTools, LocatorArgs

GITHUB_ORIGINS = ("https://github.com", "https://github.githubassets.com",
                  "https://avatars.githubusercontent.com")


class LoginRequest(Contract):
    url: str
    status_url: str
    login: Locator
    authenticated_field: str = "authenticated"
    timeout: float = Field(default=300, ge=1, le=600)


class AssistedLogin:
    def __init__(self, project, *, allow_origins=(), progress=print):
        self.project = project
        self.allow_origins = tuple(allow_origins)
        self.progress = progress
        self.state = None
        self.request = None

    async def run(self, request: LoginRequest):
        self.state = None
        self.request = None
        if LocalTools.origin(request.url) != LocalTools.origin(request.status_url):
            raise ValueError("Login status endpoint must have the same origin as the app")
        # No trace, video, screenshot, provider cookies or status payload is exported.
        with tempfile.TemporaryDirectory(prefix="ov-login-") as directory:
            tools = LocalTools(self.project, Artifacts(Path(directory)), headless=False,
                               allow_origins=(*self.allow_origins, *GITHUB_ORIGINS),
                               trace_browser=False)
            tools.check_url(request.url)
            try:
                page = await tools.browser_page()
                await page.goto(request.url, wait_until="domcontentloaded")
                await tools._locator(LocatorArgs(**request.login.model_dump())).click()
                self.progress("Login: complete sign-in and MFA in the open browser. Waiting for the app to confirm your session…")
                async with asyncio.timeout(request.timeout):
                    while True:
                        if page.is_closed():
                            raise ValueError("Login browser was closed; retry assisted_login to reopen it")
                        if LocalTools.origin(page.url) == LocalTools.origin(request.url):
                            if await self.confirm(tools.context, request):
                                state = await tools.context.storage_state()
                                host = urlsplit(request.url).hostname
                                self.state = {
                                    "cookies": [c for c in state["cookies"]
                                                if c["domain"].lstrip(".") == host],
                                    "origins": [o for o in state["origins"]
                                                if LocalTools.origin(o["origin"]) == LocalTools.origin(request.url)],
                                }
                                self.request = request
                                self.progress("Login: authenticated session confirmed; continuing tests.")
                                return {"authenticated": True, "assisted": True,
                                        "origin": LocalTools.origin(request.url)}
                        await asyncio.sleep(2)
            except TimeoutError:
                raise ValueError("Login timed out. Retry assisted_login or mark authenticated cases blocked.") from None
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Login browser failed ({type(exc).__name__}); retry or check local OAuth setup.") from None
            finally:
                await tools.close()

    @staticmethod
    async def confirm(context, request):
        try:
            cookies = {c["name"]: c["value"] for c in await context.cookies(request.status_url)}
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3,
                                         cookies=cookies) as client:
                response = await client.get(request.status_url)
                return response.status_code == 200 and response.json().get(request.authenticated_field) is True
        except Exception:
            return False

    async def verify(self, context, url):
        return (self.state is not None and self.request is not None
                and LocalTools.origin(url) == LocalTools.origin(self.request.url)
                and await self.confirm(context, self.request))
