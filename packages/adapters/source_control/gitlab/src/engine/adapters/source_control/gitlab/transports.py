"""OAuth transport for the GitLab REST API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx


class GitLabTransportError(RuntimeError):
    """A GitLab REST request failed."""


class GitLabOAuthTransport:
    """GitLab REST transport that refreshes an expired bearer token once."""

    def __init__(
        self,
        token: str | Callable[[], str | None],
        origin: str | Callable[[], str] = "https://gitlab.com",
        on_token_unauthorized: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._token_source = token
        self._origin_source = origin
        self._on_token_unauthorized = on_token_unauthorized

    @property
    def _token(self) -> str:
        return self._token_source() or "" if callable(self._token_source) else self._token_source

    @property
    def _origin(self) -> str:
        return (self._origin_source() if callable(self._origin_source) else self._origin_source).rstrip("/")

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        token = self._token
        response = await self._send(method, path, token, **kwargs)
        if response.status_code == 401 and token and self._on_token_unauthorized and await self._on_token_unauthorized(token):
            response = await self._send(method, path, self._token, **kwargs)
        if response.is_error:
            raise self._error(method, path, response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise GitLabTransportError(f"GitLab API {method} {path} returned invalid JSON") from error

    async def download(self, path: str) -> bytes:
        token = self._token
        response = await self._send("GET", path, token, follow_redirects=True)
        if response.status_code == 401 and token and self._on_token_unauthorized and await self._on_token_unauthorized(token):
            response = await self._send("GET", path, self._token, follow_redirects=True)
        if response.is_error:
            raise self._error("GET", path, response)
        return response.content

    async def _send(self, method: str, path: str, token: str, **kwargs: object) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient() as client:
            return await client.request(method, f"{self._origin}/api/v4{path}", headers=headers, **kwargs)

    @staticmethod
    def _error(method: str, path: str, response: httpx.Response) -> GitLabTransportError:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text or f"HTTP {response.status_code}"
        return GitLabTransportError(f"GitLab API {method} {path} failed ({response.status_code}): {detail}")
