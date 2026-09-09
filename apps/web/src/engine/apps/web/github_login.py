"""Per-browser GitHub identity verification with session cookies.

Login cookies use a process-local signing key: a restart requires login again.
The session cookie is set after a successful OAuth callback and checked by
the status endpoint so the frontend can gate access.
"""

import base64
import hashlib
import hmac

import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

import httpx
from dotenv import dotenv_values
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

_PATH = "/api/auth/github"
_COOKIE = "engine_github_login"
_SESSION_COOKIE = "engine_session"
_TTL = 600
_SESSION_TTL = 86400  # 24 hours
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _return_to(value: str) -> str:
    # Reject browser URL normalization tricks as well as external URLs.
    decoded = unquote(value)
    if (len(value) > 2048 or not value.startswith("/") or not decoded.startswith("/") or decoded.startswith("//")
            or "\\" in decoded or any(ord(c) < 33 or ord(c) == 127 for c in decoded)):
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
    def __init__(self, config: GitHubLoginConfig | None) -> None:
        self.config = config
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

    def _make_session_cookie(self, user_id: int, login: str) -> str:
        """Build a signed session value: id|login|expires|signature."""
        expires = int(time.time()) + _SESSION_TTL
        payload = f"{user_id}|{login}|{expires}"
        return f"{payload}|{self._sign(payload)}"

    def _read_session(self, request: Request) -> dict[str, object] | None:
        """Verify and decode the session cookie, or None if invalid/expired."""
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        if not cookie or len(cookie) > 512:
            return None
        parts = cookie.split("|")
        if len(parts) != 4:
            return None
        user_id_str, login, expires_str, signature = parts
        payload = "|".join(parts[:3])
        if not secrets.compare_digest(signature.encode(), self._sign(payload).encode()):
            return None
        try:
            expires = int(expires_str)
            user_id = int(user_id_str)
        except ValueError:
            return None
        if expires <= time.time() or user_id <= 0 or not login:
            return None
        return {"id": user_id, "login": login}

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
        # Issue a session cookie and redirect to the app.
        response = RedirectResponse(pending[3], status_code=302)
        session_value = self._make_session_cookie(user["id"], user["login"])
        response.set_cookie(_SESSION_COOKIE, session_value, max_age=_SESSION_TTL,
                            path="/", secure=self._is_secure(),
                            httponly=True, samesite="lax")
        return response

    async def status(self, request: Request) -> Response:
        """Return the current session state for the frontend auth gate."""
        user = self._read_session(request)
        return JSONResponse({
            "authenticated": user is not None,
            "user": user,
            "loginRequired": self.config is not None,
        }, headers=_HEADERS)

    async def logout(self, request: Request) -> Response:
        if self._read_session(request) is None:
            return JSONResponse({"ok": True}, headers=_HEADERS)
        response = JSONResponse({"ok": True}, headers=_HEADERS)
        response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True,
                               samesite="lax", secure=self._is_secure())
        return response

    def middleware(self, app: ASGIApp) -> ASGIApp:
        """ASGI middleware that enforces session auth on the web and graph API routes.

        Unauthenticated requests to protected API endpoints receive a 401.
        Auth-related endpoints, static assets, and SPA pages are exempt.
        """
        if not self.configured:
            return app
        return _SessionAuthMiddleware(app, self)


# Paths under /api/ that must remain accessible without a session cookie so
# the login flow itself can work.
_AUTH_EXEMPT = frozenset({
    f"{_PATH}/login",
    f"{_PATH}/callback",
    f"{_PATH}/status",
    f"{_PATH}/logout",
    "/api/slack/events",  # Authenticated by the Slack signature in its handler.
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
        if self.login._read_session(request) is not None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "authentication required"},
            status_code=401,
            headers=_HEADERS,
        )
        await response(scope, receive, send)
