"""Per-browser GitHub identity verification, separate from repo credentials.

Session issuance and application access control are follow-up work (#301).
Pending logins are process-local: a restart requires starting login again.
"""

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from dotenv import dotenv_values
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

_PATH = "/api/auth/github"
_COOKIE = "engine_github_login"
_TTL = 600
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


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
        self._pending: dict[str, tuple[str, str, float]] = {}

    def routes(self) -> list[Route]:
        return [
            Route(f"{_PATH}/login", self.login),
            Route(f"{_PATH}/callback", self.callback),
        ]

    async def login(self, request: Request) -> Response:
        if self.config is None:
            return JSONResponse({"error": "GitHub login is not configured"}, 503, headers=_HEADERS)
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        if len(self._pending) >= 1024:
            return JSONResponse({"error": "Too many pending logins"}, 503, headers=_HEADERS)
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self._pending[state] = (browser, verifier, now + _TTL)
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
                            secure=self.config.redirect_uri.startswith("https:"),
                            httponly=True, samesite="lax")
        return response

    async def callback(self, request: Request) -> Response:
        pending = self._pending.get(request.query_params.get("state", ""))
        owns_cookie = pending is not None and secrets.compare_digest(
            request.cookies.get(_COOKIE, "").encode(), pending[0].encode()
        )
        response = await self._callback(request)
        response.headers.update(_HEADERS)
        if owns_cookie:
            response.delete_cookie(_COOKIE, path=_PATH, httponly=True, samesite="lax",
                                   secure=bool(self.config and self.config.redirect_uri.startswith("https:")))
        return response

    async def _callback(self, request: Request) -> Response:
        if self.config is None:
            return JSONResponse({"error": "GitHub login is not configured"}, 503)
        pending = self._pending.pop(request.query_params.get("state", ""), None)
        browser = request.cookies.get(_COOKIE, "")
        if (pending is None or pending[2] <= time.monotonic()
                or not secrets.compare_digest(browser.encode(), pending[0].encode())):
            return JSONResponse({"error": "Invalid or expired GitHub login state"}, 400)
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            return JSONResponse({"error": "GitHub authorization was not completed"}, 400)
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
            # Never reflect provider responses: they can contain credentials.
            return JSONResponse({"error": "Could not verify GitHub identity"}, 502)
        # The token is deliberately neither returned nor persisted. #301 can
        # issue a session here using this freshly verified, stable GitHub ID.
        return JSONResponse({"user": {"id": user["id"], "login": user["login"]}})
