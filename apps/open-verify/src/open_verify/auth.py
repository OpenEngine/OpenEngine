"""User-assisted browser authentication; state stays in memory for this run."""

import asyncio
import contextlib
import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from open_verify.artifacts import Artifacts
from open_verify.capture import highlight, remove_highlight
from open_verify.media import encode_login_gif
from open_verify.test_spec import LoginRequest
from open_verify.tools import LocalTools, LocatorArgs

GITHUB_ORIGINS = ("https://github.com", "https://github.githubassets.com",
                  "https://avatars.githubusercontent.com")
# GitHub can delegate the user's sign-in to Google. Keep these allowances
# scoped to the private, user-controlled login browser, not test browsers.
GOOGLE_LOGIN_ORIGINS = (
    "https://accounts.google.com",
    "https://www.gstatic.com",
    "https://ssl.gstatic.com",
    "https://fonts.gstatic.com",
    "https://fonts.googleapis.com",
    "https://lh3.googleusercontent.com",
)


class AssistedLogin:
    def __init__(self, project, *, allow_origins=(), progress=print, browser_session=None, artifacts=None):
        self.project = project
        self.allow_origins = tuple(allow_origins)
        self.progress = progress
        self.browser_session = browser_session
        self.state = None
        self.request = None
        self.artifacts = artifacts
        self.receipts = []
        self.screenshots = []
        self.omissions = []
        self.summary = ""

    async def run(self, request: LoginRequest, *, capture_media=False):
        self.state = None
        self.request = None
        self.screenshots, self.omissions, self.summary = [], [], ""
        if self.browser_session is not None and self.browser_session.headless:
            raise ValueError("Assisted login needs a visible browser; rerun without --headless")
        if LocalTools.origin(request.url) != LocalTools.origin(request.status_url):
            raise ValueError("Login status endpoint must have the same origin as the app")
        receipt = {"status": "pending", "origin": LocalTools.origin(request.url),
                   "events": [], "screenshots": [], "omissions": self.omissions}
        self.receipts.append(receipt)
        relative = f"login-{len(self.receipts):03d}"
        if self.artifacts is not None:
            (self.artifacts.path / relative).mkdir()

        def event(name, **details):
            receipt['events'].append({"event": name, "at": datetime.now(UTC).isoformat(), **details})
            if self.artifacts is not None:
                self.artifacts.write(f"{relative}/receipt.json", receipt)

        # Provider pages are never recorded; app-only checkpoints bracket the handoff.
        with tempfile.TemporaryDirectory(prefix="ov-login-") as directory:
            tools = LocalTools(self.project, Artifacts(Path(directory)), headless=False,
                               allow_origins=(*self.allow_origins, *GITHUB_ORIGINS,
                                              *GOOGLE_LOGIN_ORIGINS),
                               trace_browser=False, browser_session=self.browser_session)
            tools.check_url(request.url)
            try:
                page = await tools.browser_page()
                await page.goto(request.url, wait_until="domcontentloaded")
                if await self.status(tools.context, request) is not False:
                    raise ValueError("A login attempt requires a confirmed signed-out session first")
                event('signed_out_confirmed')
                if capture_media:
                    await self.capture(page, request, relative, '01-login-requested', receipt,
                                       target=tools._locator(LocatorArgs(**request.login.model_dump())))
                provider_origins = set()

                def navigation(outgoing):
                    if outgoing.is_navigation_request() and outgoing.frame == page.main_frame:
                        try:
                            origin = LocalTools.origin(outgoing.url)
                        except ValueError:
                            return
                        if origin != receipt['origin'] and origin not in provider_origins:
                            provider_origins.add(origin)
                            # Store origins only, never OAuth query parameters or fragments.
                            event('provider_redirect_observed', origin=origin)

                page.on('request', navigation)
                event('sign_in_requested')
                await tools._locator(LocatorArgs(**request.login.model_dump())).click()
                event('sign_in_clicked')
                self.progress("Login: complete sign-in and MFA in the open browser. Waiting for the app to confirm your session…")
                async with asyncio.timeout(request.timeout) as deadline:
                    while True:
                        if page.is_closed():
                            raise ValueError("Login browser was closed; retry assisted_login to reopen it")
                        # Browser-internal pages can appear during redirects or
                        # sign-in. They are not app callbacks; keep waiting without
                        # navigating or interrupting the user's interaction.
                        try:
                            page_origin = LocalTools.origin(page.url)
                        except ValueError:
                            page_origin = None
                        if page_origin == LocalTools.origin(request.url):
                            if await self.confirm(tools.context, request):
                                deadline.reschedule(None)  # Handoff completed; captures have their own bounds.
                                state = await tools.context.storage_state()
                                host = urlsplit(request.url).hostname
                                self.state = {
                                    "cookies": [c for c in state["cookies"]
                                                if c["domain"].lstrip(".") == host],
                                    "origins": [o for o in state["origins"]
                                                if LocalTools.origin(o["origin"]) == LocalTools.origin(request.url)],
                                }
                                self.request = request
                                event('authenticated_return_confirmed')
                                if capture_media:
                                    await self.capture(page, request, relative, '02-login-success', receipt)
                                    if self.artifacts is not None:
                                        gif = f'{relative}/login-journey-summary.gif'
                                        reason = await encode_login_gif(
                                            [self.artifacts.path / p for p in self.screenshots], self.artifacts.path / gif)
                                        if reason:
                                            self.omissions.append(reason)
                                        else:
                                            self.screenshots.append(gif)
                                            receipt['screenshots'] = list(self.screenshots)
                                            self.progress('Login: screenshot summary prepared.')
                                receipt['status'] = 'passed'
                                self.summary = (
                                    "User-assisted login passed: signed-out session confirmed, sign-in clicked, "
                                    "authenticated return confirmed. "
                                    + ("Provider redirect observed. " if provider_origins else "No external provider redirect was observed. ")
                                    + "Credential entry and MFA are excluded from captures. "
                                    + ("The GIF is a screenshot journey summary. " if capture_media else "")
                                    + ("GIF checkpoints show the app checks; orange outlines mark the next click." if capture_media else "")
                                )
                                event('login_passed')
                                self.progress("Login: authenticated session confirmed; continuing tests.")
                                return {"authenticated": True, "assisted": True,
                                        "origin": LocalTools.origin(request.url),
                                        "summary": self.summary, "events": receipt['events'],
                                        "screenshots": list(self.screenshots),
                                        "receipt": f"{relative}/receipt.json" if self.artifacts else None}
                        await asyncio.sleep(2)
            except TimeoutError:
                raise ValueError("Login timed out. Retry assisted_login or mark authenticated cases blocked.") from None
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Login browser failed ({type(exc).__name__}); retry or check local OAuth setup.") from None
            finally:
                if receipt['status'] == 'pending':
                    receipt['status'] = 'blocked'
                    event('login_not_completed')
                await tools.close()

    async def capture(self, page, request, relative, name, receipt, target=None):
        if self.artifacts is None:
            return
        app_origin = LocalTools.origin(request.url)

        def is_app_screen():
            try:
                url = urlsplit(page.url)
                keys = {key.lower() for key in (*parse_qs(url.query), *parse_qs(url.fragment))}
                return (LocalTools.origin(page.url) == app_origin
                        and 'callback' not in url.path.lower()
                        and not ({'code', 'state', 'token', 'access_token'} & keys))
            except ValueError:
                return False

        async def guard(route):
            outgoing = route.request
            if outgoing.is_navigation_request() and outgoing.frame == page.main_frame:
                try:
                    allowed = LocalTools.origin(outgoing.url) == app_origin
                except ValueError:
                    allowed = False
                if not allowed:
                    await route.abort()
                    return
            await route.fallback()

        marker = None
        try:
            await page.route('**/*', guard)
            if not is_app_screen():
                raise ValueError('not a safe app screen')
            before = page.url
            if target is not None:
                marker = await highlight(page, target)
            data = await page.screenshot(timeout=5000, animations='disabled',
                mask=[page.locator('input[type="password"], input[autocomplete="one-time-code"]')])
            if not is_app_screen() or page.url != before:
                raise ValueError('page changed during capture')
            digest = hashlib.sha256(data).digest()
            if any(hashlib.sha256((self.artifacts.path / p).read_bytes()).digest() == digest for p in self.screenshots):
                return
            path = f'{relative}/{name}.png'
            (self.artifacts.path / path).write_bytes(data)
            self.screenshots.append(path)
            receipt['screenshots'] = list(self.screenshots)
            self.progress('Login: captured ' + ('sign-in screen with the button highlighted.' if target is not None else 'authenticated return to the app.'))
        except Exception:
            self.omissions.append(f'Login checkpoint {name} unavailable; no provider screen was saved.')
        finally:
            await remove_highlight(marker)
            with contextlib.suppress(Exception):
                await page.unroute('**/*', guard)

    @staticmethod
    async def status(context, request):
        try:
            cookies = {c["name"]: c["value"] for c in await context.cookies(request.status_url)}
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3,
                                         cookies=cookies) as client:
                response = await client.get(request.status_url)
                value = response.json().get(request.authenticated_field)
                return value if response.status_code == 200 and type(value) is bool else None
        except Exception:
            return None

    @staticmethod
    async def confirm(context, request):
        return await AssistedLogin.status(context, request) is True

    async def verify(self, context, url):
        return (self.state is not None and self.request is not None
                and LocalTools.origin(url) == LocalTools.origin(self.request.url)
                and await self.confirm(context, self.request))
