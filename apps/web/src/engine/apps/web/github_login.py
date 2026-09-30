"""Per-browser GitHub identity verification with session cookies.

Login cookies use a process-local signing key: a restart requires login again.
The session cookie is set after a successful OAuth callback and checked by
the status endpoint so the frontend can gate access.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

import anyio
import httpx
from dotenv import dotenv_values
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

_PATH = "/api/auth/github"
_COOKIE = "engine_github_login"
_SESSION_COOKIE = "engine_session"
_TTL = 600
_SESSION_TTL = 86400  # 24 hours
# How long GitHub's answer about a user's repository access is trusted before a
# signed-in request asks again, so revoked access ends within this, not a day.
_ACCESS_TTL = 300
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# The one route a service credential may reach: the MCP gateway creating work
# orders. Everything else still requires a browser session.
_SERVICE_ROUTE = ("POST", "/api/runs")
# How often a response still streaming asks whether its user may keep it, so an
# open event stream ends about when a new request would be refused.
_STREAM_RECHECK = 30


def valid_service_token(token: str) -> bool:
    """Whether `token` is long enough to accept as a bearer secret."""
    return len(token) >= 32 and not any(c.isspace() for c in token)


def _return_to(value: str) -> str:
    # Reject browser URL normalization tricks as well as external URLs.
    decoded = unquote(value)
    if (len(value) > 2048 or not value.startswith("/") or not decoded.startswith("/") or decoded.startswith("//")
            or "\\" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
        return "/"
    return value


@dataclass(frozen=True)
class GitHubLoginConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    secret_file: Path | None = field(default=None, repr=False)

    def current_secret(self) -> str:
        if self.secret_file is None:
            return self.client_secret
        values = dotenv_values(self.secret_file, interpolate=False)
        secret = os.environ.get(
            "ENGINE_GITHUB_LOGIN_CLIENT_SECRET",
            values.get("ENGINE_GITHUB_LOGIN_CLIENT_SECRET") or "",
        )
        if not secret:
            raise ValueError("GitHub login secret is not configured")
        return secret

    def __post_init__(self) -> None:
        uri = urlsplit(self.redirect_uri)
        local = uri.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not self.client_id or not self.client_secret or not uri.hostname
            or (uri.scheme != "https" and not (uri.scheme == "http" and local))
            or uri.username or uri.password or uri.query or uri.fragment
            or uri.path != f"{_PATH}/callback"
        ):
            raise ValueError("GitHub login requires credentials and an HTTPS callback URL (HTTP allowed on loopback)")


class GitHubLogin:
    def __init__(
        self,
        config: GitHubLoginConfig | None,
        service_token: Callable[[], str] = lambda: "",
        authorize: Callable[[int, str], Awaitable[bool]] | None = None,
        operators: Collection[int] = frozenset(),
        authorize_user: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.config = config
        # Whether a verified GitHub account (id, login) may have a session.
        # Asked at sign-in and again, at most every _ACCESS_TTL seconds, by
        # signed-in requests, so revoked access does not outlive the cache.
        self.authorize = authorize
        # GitHub user IDs let in without asking: new people before they can
        # push anywhere, and everyone who can fix it when the server's own
        # GitHub login stops answering.
        self.operators = frozenset(operators)
        # Whether the account holding a sign-in token can write to one of the
        # repositories, asked with that token. Asked alongside `authorize` and
        # counted only when `authorize` fails, and then only as a yes, so a
        # broken server connection does not lock out everyone who could fix it.
        self.authorize_user = authorize_user
        # user id -> session id -> (the read:user token GitHub issued at that
        # sign-in, when that session's cookie expires). Kept in memory only,
        # like the signing key, for rechecks, and per session, so signing out
        # in one browser leaves the others their fallback.
        self._user_tokens: dict[int, dict[str, tuple[str, float]]] = {}
        # user id -> (allowed, monotonic expiry). Only GitHub's answers are
        # kept; a failed lookup is retried by the next request.
        self._access: dict[int, tuple[bool, float]] = {}
        # One lock per user, so one slow lookup holds up only that user.
        self._access_locks: dict[int, asyncio.Lock] = {}
        # Whether the most recent lookup through the server's connection
        # failed, shown to signed-in users.
        self.access_check_failing = False
        # A reader rather than a value, so rotating the secret on disk takes
        # effect on the next request without a restart.
        self.service_token = service_token
        self._signing_key = secrets.token_bytes(32)

    def routes(self) -> list[Route]:
        return [
            Route(f"{_PATH}/login", self.login),
            Route(f"{_PATH}/callback", self.callback),
            Route(f"{_PATH}/status", self.status),
            Route(f"{_PATH}/logout", self.logout, methods=["POST"]),
        ]

    @property
    def configured(self) -> bool:
        return self.config is not None

    def _sign(self, payload: str) -> str:
        return hmac.new(self._signing_key, payload.encode(), hashlib.sha256).hexdigest()

    def _make_session_cookie(
        self, user_id: int, login: str, session_id: str | None = None, expires: int | None = None
    ) -> str:
        """Build a signed session value: id|login|expires|session id|signature."""
        expires = int(time.time()) + _SESSION_TTL if expires is None else expires
        session_id = secrets.token_urlsafe(16) if session_id is None else session_id
        payload = f"{user_id}|{login}|{expires}|{session_id}"
        return f"{payload}|{self._sign(payload)}"

    def _read_session(self, request: Request) -> dict[str, object] | None:
        """Verify and decode the session cookie, or None if invalid/expired."""
        session = self._session(request)
        return None if session is None else session[0]

    def _session(self, request: Request) -> tuple[dict[str, object], str] | None:
        """The session cookie's user and session id, or None if invalid/expired."""
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        if not cookie or len(cookie) > 512:
            return None
        parts = cookie.split("|")
        if len(parts) != 5:
            return None
        user_id_str, login, expires_str, session_id, signature = parts
        payload = "|".join(parts[:4])
        if not secrets.compare_digest(signature.encode(), self._sign(payload).encode()):
            return None
        try:
            expires = int(expires_str)
            user_id = int(user_id_str)
        except ValueError:
            return None
        if expires <= time.time() or user_id <= 0 or not login or not session_id:
            return None
        return {"id": user_id, "login": login}, session_id

    def _user_token(self, user_id: int) -> str | None:
        """A sign-in token from one of the user's unexpired sessions, dropping expired ones."""
        now = time.time()
        for owner in list(self._user_tokens):
            sessions = self._user_tokens[owner]
            for session_id in [s for s, (_, expires) in sessions.items() if expires <= now]:
                del sessions[session_id]
            if not sessions:
                del self._user_tokens[owner]
        sessions = self._user_tokens.get(user_id)
        return next(iter(sessions.values()))[0] if sessions else None

    def _drop_user_token(self, user_id: int, session_id: str) -> None:
        sessions = self._user_tokens.get(user_id, {})
        sessions.pop(session_id, None)
        if not sessions:
            self._user_tokens.pop(user_id, None)

    async def has_access(self, user: dict[str, object], *, fresh: bool = False) -> bool | None:
        """Whether `user` may use the app: GitHub's answer, or None if it could not be had.

        `fresh` skips the cache, as a sign-in does, so newly granted access
        works at once.
        """
        user_id, login = user["id"], user["login"]
        if self.authorize is None or user_id in self.operators:
            return True
        lock = self._access_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            cached = self._access.get(user_id)
            if not fresh and cached is not None and cached[1] > time.monotonic():
                return cached[0]
            token = self._user_token(user_id)
            # Started at once rather than after a failure, so both lookups
            # share one timeout budget.
            fallback = (asyncio.ensure_future(self.authorize_user(token))
                        if token and self.authorize_user is not None else None)
            if fallback is not None:
                # Its answer is unwanted once the server's lookup answers.
                fallback.add_done_callback(lambda done: done.cancelled() or done.exception())
            try:
                allowed = await self.authorize(user_id, login)
                self.access_check_failing = False
            except Exception:
                log.exception("could not check repository access for %s", login)
                self.access_check_failing = True
                if not await self._confirmed_by_user(fallback, login):
                    # Access that cannot be confirmed is not granted, but
                    # neither is the failure remembered: the next request
                    # asks again.
                    return None
                allowed = True
            finally:
                if fallback is not None:
                    fallback.cancel()
            if not allowed:
                # Access is gone, so no session needs a fallback for it.
                self._user_tokens.pop(user_id, None)
            self._access[user_id] = (allowed, time.monotonic() + _ACCESS_TTL)
            return allowed

    @staticmethod
    async def _confirmed_by_user(fallback: asyncio.Future[bool] | None, login: str) -> bool:
        """Whether the user's own token showed write access; a failure is not a yes."""
        if fallback is None:
            return False
        try:
            confirmed = await fallback
        except Exception:
            log.exception("could not check repository access for %s with their own token", login)
            return False
        if confirmed:
            log.warning("admitted %s on their own token's answer; the server's lookup failed", login)
        return confirmed is True

    def _has_service_token(self, request: Request) -> bool:
        """Whether the request carries the configured service bearer token."""
        if (request.method, request.url.path) != _SERVICE_ROUTE:
            return False
        expected = self.service_token()
        if not valid_service_token(expected):
            return False
        headers = request.headers.getlist("authorization")
        return len(headers) == 1 and secrets.compare_digest(
            headers[0].encode(), f"Bearer {expected}".encode()
        )

    def _is_secure(self) -> bool:
        return bool(self.config and self.config.redirect_uri.startswith("https:"))

    async def login(self, request: Request) -> Response:
        if self.config is None:
            return JSONResponse({"error": "GitHub login is not configured"}, 503, headers=_HEADERS)
        state, verifier = (secrets.token_urlsafe(32) for _ in range(2))
        destination = base64.urlsafe_b64encode(
            _return_to(request.query_params.get("return_to", "/")).encode()
        ).decode()
        payload = f"{state}.{verifier}.{int(time.time()) + _TTL}.{destination}"
        signature = self._sign(payload)
        browser = f"{payload}.{signature}"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode({
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "read:user",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }), status_code=302, headers=_HEADERS)
        response.set_cookie(_COOKIE, browser, max_age=_TTL, path=_PATH,
                            secure=self._is_secure(),
                            httponly=True, samesite="lax")
        return response

    def _login_cookie(self, request: Request) -> tuple[str, str, int, str] | None:
        cookie = request.cookies.get(_COOKIE, "")
        if len(cookie) > 3072:
            return None
        parts = cookie.split(".")
        if len(parts) != 5:
            return None
        state, verifier, expires, destination, signature = parts
        payload = ".".join(parts[:4])
        if not secrets.compare_digest(signature.encode(), self._sign(payload).encode()):
            return None
        try:
            return state, verifier, int(expires), _return_to(
                base64.urlsafe_b64decode(destination).decode()
            )
        except ValueError:
            return None

    async def callback(self, request: Request) -> Response:
        pending = self._login_cookie(request)
        owns_cookie = pending is not None and secrets.compare_digest(
            request.query_params.get("state", "").encode(), pending[0].encode()
        )
        response = await self._callback(request, pending)
        if owns_cookie and pending[3] != "/" and response.headers.get("location", "").startswith("/login?error="):
            response.headers["location"] += "&" + urlencode({"return_to": pending[3]})
        response.headers.update(_HEADERS)
        if owns_cookie:
            response.delete_cookie(_COOKIE, path=_PATH, httponly=True, samesite="lax",
                                   secure=self._is_secure())
        return response

    async def _callback(
        self, request: Request, pending: tuple[str, str, int, str] | None
    ) -> Response:
        if self.config is None:
            return JSONResponse({"error": "GitHub login is not configured"}, 503)
        if (pending is None or pending[2] <= time.time()
                or not secrets.compare_digest(
                    request.query_params.get("state", "").encode(), pending[0].encode()
                )):
            return RedirectResponse("/login?error=expired", status_code=302)
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            return RedirectResponse("/login?error=denied", status_code=302)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                token_response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={"client_id": self.config.client_id,
                          "client_secret": self.config.current_secret(),
                          "redirect_uri": self.config.redirect_uri,
                          "code": code, "code_verifier": pending[1]},
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                token_body = token_response.json()
                if not isinstance(token_body, dict) or token_body.get("error"):
                    raise ValueError("Invalid token response")
                token = token_body.get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("Missing token")
                user_response = await client.get(
                    "https://api.github.com/user",
                    headers={"Accept": "application/vnd.github+json",
                             "Authorization": f"Bearer {token}"},
                )
                user_response.raise_for_status()
                user = user_response.json()
                if (not isinstance(user, dict) or type(user.get("id")) is not int
                        or user["id"] <= 0 or not isinstance(user.get("login"), str)
                        or not user["login"]):
                    raise ValueError("Invalid identity")
        except (httpx.HTTPError, ValueError, OSError):
            return RedirectResponse("/login?error=failed", status_code=302)
        # Bound to the ID GitHub just returned for this token, and kept only
        # as long as the session cookie issued for it.
        session_id = secrets.token_urlsafe(16)
        expires = int(time.time()) + _SESSION_TTL
        self._user_tokens.setdefault(user["id"], {})[session_id] = (token, expires)
        allowed = await self.has_access(user, fresh=True)
        if not allowed:
            self._drop_user_token(user["id"], session_id)
        if allowed is None:
            # The identity is verified; only the permission check failed.
            return RedirectResponse("/login?error=unverified", status_code=302)
        if not allowed:
            log.info("refused a session to %s, who cannot write to any repository", user["login"])
            return RedirectResponse("/login?error=forbidden", status_code=302)
        # Issue a session cookie and redirect to the app.
        response = RedirectResponse(pending[3], status_code=302)
        session_value = self._make_session_cookie(user["id"], user["login"], session_id, expires)
        response.set_cookie(_SESSION_COOKIE, session_value, max_age=_SESSION_TTL,
                            path="/", secure=self._is_secure(),
                            httponly=True, samesite="lax")
        return response

    async def status(self, request: Request) -> Response:
        """Return the current session state for the frontend auth gate."""
        user = self._read_session(request)
        allowed = None if user is None else await self.has_access(user)
        if user is not None and allowed is None:
            # Not signed out, but not confirmed either: the browser keeps its
            # page and asks again, as it does for any failed check.
            return JSONResponse({"error": "repository access could not be verified"},
                                503, headers=_HEADERS)
        revoked = allowed is False
        if revoked:
            user = None
        body: dict[str, object] = {
            "authenticated": user is not None,
            "user": user,
            "loginRequired": self.config is not None,
        }
        if user is not None:
            # Whoever is signed in may be able to fix the server's connection;
            # anyone it cannot vouch for is refused while this is true.
            body["accessCheckFailing"] = self.access_check_failing
        response = JSONResponse(body, headers=_HEADERS)
        if revoked:
            response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True,
                                   samesite="lax", secure=self._is_secure())
        return response

    async def logout(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return JSONResponse({"ok": True}, headers=_HEADERS)
        self._drop_user_token(int(session[0]["id"]), session[1])
        response = JSONResponse({"ok": True}, headers=_HEADERS)
        response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True,
                               samesite="lax", secure=self._is_secure())
        return response

    def middleware(self, app: ASGIApp) -> ASGIApp:
        """ASGI middleware that enforces session auth on the web and graph API routes.

        Unauthenticated requests to protected API endpoints receive a 401.
        Auth-related endpoints, static assets, and SPA pages are exempt. The
        service token admits only `POST /api/runs`, for the MCP gateway.
        """
        if not self.configured:
            return app
        return _SessionAuthMiddleware(app, self)


# Paths under /api/ that must remain accessible without a session cookie so
# the login flow itself can work.
_AUTH_EXEMPT = frozenset({
    "/api/health",  # Public service identity and readiness; no user data.
    f"{_PATH}/login",
    f"{_PATH}/callback",
    f"{_PATH}/status",
    f"{_PATH}/logout",
    "/api/slack/events",  # Authenticated by the Slack signature in its handler.
    "/api/github/events",  # Authenticated by the GitHub signature in its handler.
})


class _SessionAuthMiddleware:
    def __init__(self, app: ASGIApp, login: GitHubLogin) -> None:
        self.app = app
        self.login = login

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if not path.startswith(("/api/", "/graph/api/")) or path in _AUTH_EXEMPT:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.login._has_service_token(request):
            await self.app(scope, receive, send)
            return
        user = self.login._read_session(request)
        allowed = None if user is None else await self.login.has_access(user)
        if allowed:
            await self._serve(request, scope, receive, send)
            return
        if user is not None and allowed is None:
            response = JSONResponse(
                {"error": "repository access could not be verified"},
                status_code=503,
                headers=_HEADERS,
            )
        else:
            response = JSONResponse(
                {"error": "authentication required"},
                status_code=401,
                headers=_HEADERS,
            )
        await response(scope, receive, send)

    async def _serve(self, request: Request, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app, ending its response if access ends while it streams."""
        started = finished = revoked = False
        error: Exception | None = None

        async def tracked(message: Message) -> None:
            nonlocal started, finished
            started = True
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)

        async with anyio.create_task_group() as group:
            async def serve() -> None:
                nonlocal error
                try:
                    await self.app(scope, receive, tracked)
                except Exception as exc:
                    # Raised as itself below, not inside an exception group.
                    error = exc
                finally:
                    group.cancel_scope.cancel()

            group.start_soon(serve)
            while True:
                await anyio.sleep(_STREAM_RECHECK)
                user = self.login._read_session(request)
                if user is None or not await self.login.has_access(user):
                    revoked = True
                    group.cancel_scope.cancel()
                    break
        if error is not None:
            raise error
        if not revoked or finished:
            return
        log.info("ended a response whose session no longer has access")
        if started:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await JSONResponse({"error": "authentication required"}, 401,
                               headers=_HEADERS)(scope, receive, send)
