"""Authenticated transports for the GitHub REST API.

The source-control adapter owns GitHub resource semantics and error messages.
These transports only execute a request using either an OAuth bearer token or
the credentials already managed by ``gh auth``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import httpx


class GitHubTransportError(RuntimeError):
    """One transport could not complete a GitHub API request."""


class GitHubApiTransport(Protocol):
    async def request(self, method: str, path: str, **kwargs: object) -> object: ...

    async def download(self, path: str) -> bytes: ...


class GitHubOAuthTransport:
    """GitHub REST transport authenticated by a token supplier."""

    def __init__(
        self,
        token: str | Callable[[], str | None],
        api_url: str = "https://api.github.com",
        on_token_unauthorized: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._token_source = token
        self._api_url = api_url.rstrip("/")
        self._on_token_unauthorized = on_token_unauthorized

    @property
    def _token(self) -> str:
        return (
            self._token_source() or ""
            if callable(self._token_source)
            else self._token_source
        )

    def _headers(self, token: str) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        token = self._token
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method, f"{self._api_url}{path}", headers=self._headers(token), **kwargs
            )
        if await self._refresh_after_unauthorized(response, token):
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    f"{self._api_url}{path}",
                    headers=self._headers(self._token),
                    **kwargs,
                )
        if response.is_error:
            raise self._request_error(method, path, response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise GitHubTransportError(
                f"GitHub API {method} {path} returned invalid JSON"
            ) from error

    async def download(self, path: str) -> bytes:
        token = self._token
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                f"{self._api_url}{path}", headers=self._headers(token)
            )
        if await self._refresh_after_unauthorized(response, token):
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(
                    f"{self._api_url}{path}", headers=self._headers(self._token)
                )
        if response.is_error:
            raise self._request_error("GET", path, response)
        return response.content

    async def _refresh_after_unauthorized(
        self, response: httpx.Response, failed_token: str
    ) -> bool:
        """Refresh once after a 401, leaving other HTTP errors untouched."""
        return bool(
            response.status_code == 401
            and failed_token
            and self._on_token_unauthorized is not None
            and await self._on_token_unauthorized(failed_token)
        )

    @staticmethod
    def _request_error(
        method: str, path: str, response: httpx.Response
    ) -> GitHubTransportError:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text or f"HTTP {response.status_code}"
        return GitHubTransportError(
            f"GitHub API {method} {path} failed ({response.status_code}): {detail}"
        )


class GitHubCliTransport:
    """GitHub REST transport delegated to the user's authenticated ``gh`` CLI."""

    def __init__(self, binary_path: str = "gh") -> None:
        self._binary_path = binary_path

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        arguments = [
            "api",
            path,
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        params = kwargs.get("params")
        if isinstance(params, Mapping):
            for key, value in params.items():
                arguments.extend(["--raw-field", f"{key}={value}"])
        body: bytes | None = None
        payload = kwargs.get("json")
        if payload is not None:
            arguments.extend(["--input", "-"])
            body = json.dumps(payload).encode()
        output = await self._run(*arguments, input_bytes=body)
        if not output.strip():
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise GitHubTransportError("gh returned a non-JSON API response") from error

    async def download(self, path: str) -> bytes:
        return await self._run("api", path, "--method", "GET")

    async def _run(self, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary_path,
                *arguments,
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise GitHubTransportError(
                "GitHub CLI is not installed; install it and run 'gh auth login', "
                "or select GitHub OAuth in Settings"
            ) from error
        except OSError as error:
            raise GitHubTransportError(
                f"could not start {self._binary_path}: {error}"
            ) from error
        stdout, stderr = await process.communicate(input_bytes)
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            if "not logged into" in detail.lower() or "authenticate" in detail.lower():
                detail = (
                    "GitHub CLI is not authenticated; run 'gh auth login' and try again"
                )
            raise GitHubTransportError(detail or "gh API request failed")
        return stdout


__all__ = [
    "GitHubApiTransport",
    "GitHubCliTransport",
    "GitHubOAuthTransport",
    "GitHubTransportError",
]
